"""PowerSync Update Entity.

Polls the GitHub Releases API to detect new versions and surfaces them
through HA's native Updates panel (Settings → System → Updates).

No authentication required — uses the public GitHub API.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

GITHUB_REPO = "bolagnaise/PowerSync"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
SCAN_INTERVAL = timedelta(hours=1)


def _installed_version() -> str:
    """Read installed version from manifest.json."""
    import json
    from pathlib import Path

    manifest_path = Path(__file__).parent / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
        return manifest.get("version", "0.0.0")
    except Exception:
        return "0.0.0"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PowerSync update entity."""
    coordinator = PowerSyncUpdateCoordinator(hass)
    await coordinator.async_config_entry_first_refresh()
    installed_version = await hass.async_add_executor_job(_installed_version)
    async_add_entities([PowerSyncUpdateEntity(coordinator, entry, installed_version)])


class PowerSyncUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls GitHub Releases API."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="PowerSync Update Check",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch latest release from GitHub."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                GITHUB_API_URL,
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 403:
                    _LOGGER.debug("GitHub API rate limited, will retry next cycle")
                    return self.data or {}
                if resp.status != 200:
                    raise UpdateFailed(f"GitHub API returned {resp.status}")
                data = await resp.json()
                tag = data.get("tag_name", "")
                version = tag.lstrip("v")
                return {
                    "latest_version": version,
                    "release_url": data.get("html_url", ""),
                    "release_notes": data.get("body", ""),
                    "published_at": data.get("published_at", ""),
                }
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"GitHub API request failed") from err


class PowerSyncUpdateEntity(CoordinatorEntity, UpdateEntity):
    """Update entity for PowerSync."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.RELEASE_NOTES
    _attr_has_entity_name = True
    _attr_name = "Update"

    def __init__(
        self,
        coordinator: PowerSyncUpdateCoordinator,
        entry: ConfigEntry,
        installed_version: str,
    ) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_update"
        self._installed = installed_version

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "PowerSync",
            "manufacturer": "PowerSync",
        }

    @property
    def installed_version(self) -> str | None:
        """Return the installed version."""
        return self._installed

    @property
    def latest_version(self) -> str | None:
        """Return the latest available version."""
        if self.coordinator.data:
            return self.coordinator.data.get("latest_version")
        return self._installed

    @property
    def release_url(self) -> str | None:
        """Return the release URL."""
        if self.coordinator.data:
            return self.coordinator.data.get("release_url")
        return None

    async def async_release_notes(self) -> str | None:
        """Return the release notes."""
        if self.coordinator.data:
            return self.coordinator.data.get("release_notes")
        return None
