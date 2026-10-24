"""Serialize battery routes, allowing cleanup nested in the same HA task."""
import asyncio


class BatteryRouteLock:
    """Reentrant for one task only; child tasks never inherit ownership."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner = None
        self._depth = 0

    def locked(self):
        return self._lock.locked()

    async def __aenter__(self):
        task = asyncio.current_task()
        if task is not self._owner:
            await self._lock.acquire()
            self._owner = task
        self._depth += 1
        return self

    async def __aexit__(self, *_):
        if asyncio.current_task() is not self._owner or self._depth <= 0:
            raise RuntimeError("Battery route lock released by a different task")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()
