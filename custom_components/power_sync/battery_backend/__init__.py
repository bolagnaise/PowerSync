"""Battery connection profiles and upstream sensor discovery."""

from .profiles import (
    BatteryConnectionProfile,
    profiles_for_system,
    resolve_connection_profile,
)

__all__ = [
    "BatteryConnectionProfile",
    "profiles_for_system",
    "resolve_connection_profile",
]
