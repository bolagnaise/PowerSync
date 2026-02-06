"""
Optimizer Configurator for PowerSync (deprecated).

This module previously integrated with HAEO (Home Assistant Energy Optimizer).
The built-in LP optimizer now handles optimization directly — no external
integration is required.

This file is kept for backward compatibility during migration. The
OptimizerConfigurator class is no longer used by the coordinator.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class OptimizerConfigurator:
    """Legacy optimizer configurator (deprecated).

    Previously auto-configured HAEO from PowerSync settings.
    Now a no-op stub for backward compatibility.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry

    async def ensure_optimizer_installed(self) -> bool:
        """Check if external optimizer is available (deprecated — always False)."""
        _LOGGER.debug(
            "OptimizerConfigurator.ensure_optimizer_installed() called — "
            "this is deprecated. The built-in LP optimizer is now used."
        )
        return False

    async def get_optimizer_network(self) -> dict | None:
        """Get existing optimizer network (deprecated)."""
        return None

    async def create_optimizer_network(self, battery_config: dict) -> str | None:
        """Create optimizer network (deprecated — no-op)."""
        return None

    async def update_optimizer_network(self, battery_config: dict) -> bool:
        """Update optimizer network (deprecated — no-op)."""
        return False

    async def delete_optimizer_network(self) -> bool:
        """Delete optimizer network (deprecated — no-op)."""
        return True
