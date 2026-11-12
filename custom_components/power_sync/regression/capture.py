"""Default-off, memory-only solver observation. Capture failure is expendable.

Attach a recorder explicitly through ``record_cycle`` in a local test/developer
harness, or to a coordinator's ``_regression_recorder``. No transport, files,
credentials, HA services or device clients are used here.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from inspect import signature
import re
import time

from .contract import (
    Codec, ContractError, INPUT_FIELDS, MAX_BYTES, SOLVE_MUTATIONS, VERSION,
    canonical, digest, schedule_snapshot, state_snapshot,
)


class CaptureRecorder:
    """Bounded whole-cycle admission; export is explicit and off the solve path."""

    def __init__(self, source, *, currency, max_cycles=4, max_bytes=MAX_BYTES):
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ContractError("currency_required")
        if not 1 <= max_cycles <= 32 or not 1024 <= max_bytes <= MAX_BYTES:
            raise ContractError("invalid_budget")
        if set(source) != {"component_sha256", "requirements_sha256", "python", "platform", "solver", "solver_version"}:
            raise ContractError("invalid_source_binding")
        for key, value in source.items():
            pattern = r"[a-f0-9]{64}" if key.endswith("sha256") else r"[a-zA-Z0-9_.+-]{1,64}"
            if type(value) is not str or not re.fullmatch(pattern, value):
                raise ContractError("invalid_source_binding")
        self.source, self.currency = dict(source), currency
        self.max_cycles, self.max_bytes = max_cycles, max_bytes
        self.cycles = deque()
        self.active = None
        self.generation = 0
        self.errors = 0
        self.dropped = 0
        self.capture_seconds = 0.0
        self._retained_bytes = 0

    def start_cycle(self):
        self.generation += 1
        if self.active is not None:
            self.dropped += 1
        self.active = {"generation": self.generation, "attempts": [], "stages": [],
                       "complete": True, "bytes": 0, "outstanding": 0}
        if len(self.cycles) >= self.max_cycles or self._retained_bytes >= self.max_bytes:
            self._drop()

    def _drop(self):
        if self.active is not None and self.active["complete"]:
            self.dropped += 1
            self.active.update(complete=False, attempts=[], stages=[], bytes=0)

    def begin(self, optimizer, arguments):
        start = time.monotonic()
        try:
            if self.active is None or not self.active["complete"]:
                return None
            if set(arguments) != set(INPUT_FIELDS):
                raise ContractError("unsupported_signature")
            codec = Codec()
            inputs = {key: codec.encode(arguments[key], key) for key in INPUT_FIELDS}
            state = state_snapshot(optimizer, codec)
            if self._retained_bytes + self.active["bytes"] + codec.bytes > self.max_bytes:
                self._drop()
                return None
            self.active["outstanding"] += 1
            return {"generation": self.generation, "codec": codec, "inputs": inputs,
                    "state": state, "decision_time": datetime.now(timezone.utc).isoformat(),
                    "arguments": arguments}
        finally:
            self.capture_seconds += time.monotonic() - start

    def finish(self, ticket, optimizer, result=None, error=None):
        start = time.monotonic()
        try:
            if ticket is None or self.active is None or ticket["generation"] != self.generation:
                return
            if not self.active["complete"]:
                return
            self.active["outstanding"] -= 1
            codec = ticket["codec"]
            after = state_snapshot(optimizer, codec)
            now_inputs = {key: codec.encode(ticket["arguments"][key], key) for key in INPUT_FIELDS}
            torn = now_inputs != ticket["inputs"] or any(
                after.get(key) != value for key, value in ticket["state"].items() if key not in SOLVE_MUTATIONS
            )
            rows = schedule_snapshot(result.schedule, codec) if result is not None else []
            if self._retained_bytes + self.active["bytes"] + codec.bytes > self.max_bytes:
                self._drop()
                return
            exception = None
            if error is not None:
                name = type(error).__name__
                exception = name if name in ("ValueError", "RuntimeError", "TimeoutError", "CancelledError", "KeyboardInterrupt") else "other"
            output = {"schedule": rows, "feasible": bool(result.feasible) if result is not None else None,
                      "solver": result.solver_used if result is not None and result.solver_used in ("highs", "greedy") else None,
                      "exception": exception}
            body = {"schema_version": VERSION, "layer": "solver_v1", "source": self.source,
                    "decision_time": ticket["decision_time"], "currency": self.currency,
                    "inputs": ticket["inputs"], "state": ticket["state"],
                    "outcome": "exception" if error is not None else ("feasible" if result.feasible else "infeasible"),
                    "result": output, "coverage": {"complete": not torn and bool(ticket["arguments"].get("schedule_timestamps")),
                        "torn": torn, "stages": ["solver_raw"], "not_covered": ["coordinator", "hardware", "counterfactual"]}}
            self.active["attempts"].append(body)
            self.active["bytes"] += codec.bytes
        finally:
            self.capture_seconds += time.monotonic() - start

    def record_stage(self, stage, schedule):
        if self.active is None or not self.active["complete"]:
            return
        if stage not in ("committed", "post_solve"):
            raise ContractError("unsupported_stage")
        codec = Codec()
        rows = schedule_snapshot(schedule, codec)
        if self._retained_bytes + self.active["bytes"] + codec.bytes > self.max_bytes:
            self._drop()
            return
        self.active["stages"].append({"stage": stage, "schedule": rows})
        self.active["bytes"] += codec.bytes

    def finish_cycle(self, outcome):
        if self.active is None:
            return
        cycle, self.active = self.active, None
        if outcome not in ("committed", "aborted"):
            raise ContractError("unsupported_cycle_outcome")
        cycle["outcome"] = outcome
        if cycle["outstanding"] or not cycle["complete"] or not cycle["attempts"]:
            if cycle["complete"]:
                self.dropped += 1
            return
        self.cycles.append(cycle)
        self._retained_bytes += cycle["bytes"]

    def export(self):
        """Canonicalize only when explicitly exported, never while solving."""
        cycles = []
        for cycle in self.cycles:
            attempts = []
            for body in cycle["attempts"]:
                attempts.append({**body, "digest": digest(body)})
            cycles.append({"outcome": cycle["outcome"], "attempts": attempts, "stages": cycle["stages"]})
        # Return detached primitives: callers cannot mutate the recorder.
        import json
        return json.loads(canonical({"schema_version": VERSION, "cycles": cycles,
            "errors": self.errors, "dropped_cycles": self.dropped,
            "capture_seconds": self.capture_seconds, "estimated_bytes": self._retained_bytes}))


def capture_attempt(function):
    """Keep the original call, arguments, result and exceptions untouched."""
    call_signature = signature(function)

    @wraps(function)
    def observed(self, *args, **kwargs):
        recorder = getattr(self, "_regression_recorder", None)
        if recorder is None:
            return function(self, *args, **kwargs)
        ticket = None
        try:
            options = function.__globals__.get("_SOLVER_BENCHMARK")
            if options is not None and options.get() is not None:
                raise ContractError("unsupported_solver_options")
            bound = call_signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            ticket = recorder.begin(self, {k: v for k, v in bound.arguments.items() if k != "self"})
        except Exception:
            recorder.errors += 1
            recorder._drop()
        try:
            result = function(self, *args, **kwargs)
        except BaseException as error:
            try:
                recorder.finish(ticket, self, error=error)
            except Exception:
                recorder.errors += 1
                recorder._drop()
            raise
        try:
            recorder.finish(ticket, self, result=result)
        except Exception:
            recorder.errors += 1
            recorder._drop()
        return result

    return observed


@contextmanager
def record_cycle(optimizer, recorder):
    """Local developer/test activation, restoring any previous observer."""
    prior = getattr(optimizer, "_regression_recorder", None)
    optimizer._regression_recorder = recorder
    recorder.start_cycle()
    try:
        yield
    finally:
        recorder.finish_cycle("aborted")
        optimizer._regression_recorder = prior
