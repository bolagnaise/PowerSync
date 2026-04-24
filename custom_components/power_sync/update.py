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
        self._notified_version: str | None = None

    async def async_added_to_hass(self) -> None:
        """Run when entity is added — check for update immediately."""
        await super().async_added_to_hass()
        self._check_and_notify()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._check_and_notify()
        super()._handle_coordinator_update()

    def _check_and_notify(self) -> None:
        """Create persistent notification if a newer version is available."""
        if not self.coordinator.data or not self.hass:
            return
        latest = self.coordinator.data.get("latest_version")
        if not latest or latest == self._installed or latest == self._notified_version:
            return
        # Only notify for newer versions, not downgrades
        try:
            from packaging.version import Version
            if Version(latest) <= Version(self._installed):
                return
        except Exception:
            if latest <= self._installed:
                return
        self._notified_version = latest
        release_url = self.coordinator.data.get("release_url", "")
        notes_line = f"\n\n[View release notes]({release_url})" if release_url else ""
        self.hass.async_create_task(
            self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "PowerSync Update Available",
                    "message": (
                        f"PowerSync **v{latest}** is available "
                        f"(installed: v{self._installed}).{notes_line}"
                    ),
                    "notification_id": f"power_sync_update_{latest}",
                },
            )
        )
        _LOGGER.info("PowerSync update available: v%s → v%s", self._installed, latest)

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
