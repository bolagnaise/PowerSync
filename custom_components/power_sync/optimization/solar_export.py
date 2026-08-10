"""Persisted lifecycle for Profit Max solar-export charge holds."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SolarExportCapability:
    """Resolved hardware capability for a zero-charge solar-export hold."""

    supported: bool
    reason: str
    export_limit_kw: float | None = None
    adapter: str | None = None


class SolarExportHoldController:
    """Own and recover the temporary zero-charge hardware limit."""

    def __init__(self, store: Any, adapter: Any) -> None:
        self._store = store
        self._adapter = adapter
        self._state: dict[str, Any] = {}

    @property
    def active(self) -> bool:
        """Return whether a hold may still be applied to hardware."""
        return self._state.get("phase") in {
            "apply_pending",
            "active",
            "clear_pending",
        }

    @property
    def status(self) -> dict[str, Any]:
        """Return a safe copy of persisted lifecycle diagnostics."""
        return dict(self._state)

    async def async_reconcile_startup(self) -> bool:
        """Clear any hold left by a restart before optimization resumes."""
        loaded = await self._store.async_load()
        self._state = dict(loaded or {})
        if not self.active:
            return True
        _LOGGER.warning(
            "Profit Max: recovering stale solar-export hold from phase %s",
            self._state.get("phase"),
        )
        return await self.clear("startup_reconciliation")

    async def apply(self, owner_id: str, plan_generation: str) -> bool:
        """Apply a hold idempotently and persist ownership around hardware I/O."""
        if (
            self._state.get("phase") == "active"
            and self._state.get("owner_id") == owner_id
            and self._state.get("plan_generation") == plan_generation
        ):
            return True

        self._state = {
            "phase": "apply_pending",
            "owner_id": owner_id,
            "plan_generation": plan_generation,
            "adapter": "sigenergy",
        }
        await self._store.async_save(self._state)
        try:
            applied = bool(await self._adapter.enter_solar_export_hold(owner_id))
        except Exception as err:  # Hardware boundary: compensate and retain evidence.
            applied = False
            self._state["last_error"] = str(err)

        if applied:
            self._state["phase"] = "active"
            self._state.pop("last_error", None)
            await self._store.async_save(self._state)
            return True

        self._state["phase"] = "clear_pending"
        self._state.setdefault("last_error", "apply_failed")
        await self._store.async_save(self._state)
        await self.clear("apply_compensation")
        return False

    async def clear(self, reason: str) -> bool:
        """Restore normal charge capacity; retain retry state on failure."""
        if not self.active:
            return True
        self._state["phase"] = "clear_pending"
        self._state["clear_reason"] = reason
        await self._store.async_save(self._state)
        try:
            cleared = bool(
                await self._adapter.exit_solar_export_hold(
                    self._state.get("owner_id")
                )
            )
        except Exception as err:  # Hardware boundary: persist for next retry/startup.
            cleared = False
            self._state["last_error"] = str(err)

        if cleared:
            self._state = {}
            await self._store.async_save(self._state)
            return True

        self._state.setdefault("last_error", "clear_failed")
        await self._store.async_save(self._state)
        return False
