"""Powerwall off-grid as a last-resort curtailment method.

Some inverters cannot be curtailed at all — Enphase running the AGF profile
only supports zero-export (not load-following), older SolarEdge firmware
ignores remote power limits, and some installs simply have no supported
inverter brand. For those users, when excess solar would be exported at
negative prices (or during demand-charge windows, or while an AEMO spike
forces an export cap), the only remaining lever is to physically open the
Powerwall's grid contactor so nothing flows to the utility. The Powerwall
firmware then auto-throttles solar in islanded mode to match house load +
battery charging, which is exactly what curtailment is supposed to achieve.

This module wraps that fallback in a state machine with safety gates:

- Opt-in per entry (``CONF_POWERWALL_OFFGRID_AS_CURTAILMENT``)
- Requires a verified pairing (``CONF_POWERWALL_LOCAL_PAIRED``)
- SOC must be above a floor (default 40%) — higher than the manual off-grid
  floor because the house will run on battery until the trigger clears
- Cumulative daily duration cap (default 6h) prevents a sticky trigger
  from draining the battery on repeat cycles
- Tracks the reason ("negative_price" / "demand_charge" / "aemo_cap") so
  reconnect logic only releases what it owned
- Idempotent: ``activate`` and ``release`` are safe to call repeatedly
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_POWERWALL_LOCAL_PAIRED,
    CONF_POWERWALL_OFFGRID_AS_CURTAILMENT,
    CONF_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS,
    CONF_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC,
    DEFAULT_POWERWALL_OFFGRID_AS_CURTAILMENT,
    DEFAULT_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS,
    DEFAULT_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC,
    DOMAIN,
)
from .exceptions import PowerwallLocalError

if TYPE_CHECKING:
    from .coordinator import PowerwallLocalCoordinator

_LOGGER = logging.getLogger(__name__)

# SOC threshold for automated off-grid trigger. Only curtail when the
# battery is essentially full — below this, charging the battery from
# cheap/free solar is more valuable than islanding. The user-configurable
# floor (CONF_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC) is the SAFETY floor
# for emergency reconnect during an active session, not the trigger.
_OFFGRID_FULL_SOC_THRESHOLD = 98.0

CoordinatorGetter = Callable[[], "PowerwallLocalCoordinator | None"]


@dataclass
class CurtailmentFallbackStatus:
    """Serializable snapshot for logging + app diagnostics."""

    active: bool
    reason: str | None
    started_at: float | None
    daily_duration_s: float
    daily_cap_s: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "started_at": self.started_at,
            "daily_duration_s": round(self.daily_duration_s, 1),
            "daily_cap_s": self.daily_cap_s,
            "daily_remaining_s": max(0, self.daily_cap_s - int(self.daily_duration_s)),
        }


class PowerwallCurtailmentFallback:
    """State machine owning the off-grid-as-curtailment path for one entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator_getter: CoordinatorGetter,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._get_coordinator = coordinator_getter

        self._active = False
        self._started_at: float | None = None
        self._reason: str | None = None
        # Running cumulative total of past sessions today (excluding the
        # current active one, which is added on demand in ``_within_daily_cap``).
        self._daily_duration_s: float = 0.0
        self._daily_reset_date: date | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def reason(self) -> str | None:
        return self._reason

    def status(self) -> CurtailmentFallbackStatus:
        self._maybe_reset_daily_counter()
        return CurtailmentFallbackStatus(
            active=self._active,
            reason=self._reason,
            started_at=self._started_at,
            daily_duration_s=self._daily_duration_s + self._current_session_seconds(),
            daily_cap_s=self._daily_cap_seconds(),
        )

    # ── Public control API ──────────────────────────────────────────

    async def activate(self, reason: str) -> bool:
        """Open the grid contactor as a curtailment fallback.

        Returns True if the Powerwall is now off-grid due to this fallback
        (either newly disconnected, or already disconnected from a prior
        call). Returns False if any safety gate refused the attempt — in
        which case the caller should fall back to whatever was next on its
        own error path.
        """
        if not self._is_enabled():
            return False
        if self._active:
            # Idempotent — already in the state the caller wanted.
            return True
        if not self._is_paired():
            _LOGGER.debug(
                "Curtailment fallback: skipped (Powerwall not paired)"
            )
            return False
        soc_ok, soc_val, floor = self._check_soc_floor()
        if not soc_ok:
            _LOGGER.info(
                "Curtailment fallback: skipped — SOC %s%% below floor %s%%",
                soc_val,
                floor,
            )
            return False
        # Only trigger when battery is essentially full — otherwise the
        # correct response to negative prices is to charge the battery,
        # not to island and waste the cheap solar.
        if soc_val is None or soc_val < _OFFGRID_FULL_SOC_THRESHOLD:
            _LOGGER.debug(
                "Curtailment fallback: skipped — SOC %s%% below full "
                "threshold %s%% (charge the battery instead)",
                soc_val, _OFFGRID_FULL_SOC_THRESHOLD,
            )
            return False
        if not self._within_daily_cap():
            _LOGGER.info(
                "Curtailment fallback: skipped — daily cap %ss already used",
                self._daily_cap_seconds(),
            )
            return False

        coord = self._get_coordinator()
        if coord is None or coord.client is None:
            _LOGGER.warning(
                "Curtailment fallback: paired but local client unavailable"
            )
            return False

        try:
            ok = await coord.client.go_off_grid()
        except PowerwallLocalError as err:
            _LOGGER.error("Curtailment fallback: go_off_grid failed: %s", err)
            return False
        except Exception as err:
            _LOGGER.error(
                "Curtailment fallback: unexpected curtail error: %s",
                err,
                exc_info=True,
            )
            return False

        if not ok:
            _LOGGER.warning(
                "Curtailment fallback: go_off_grid command failed"
            )
            return False

        self._active = True
        self._started_at = time.time()
        self._reason = reason
        _LOGGER.info(
            "⚡ Powerwall off-grid curtailment ACTIVATED (reason=%s, soc=%s%%)",
            reason,
            soc_val,
        )
        # Trigger an immediate coordinator refresh so entities/UI show the
        # new islanded state without waiting the 10s poll interval.
        try:
            await coord.async_request_refresh()
        except Exception:  # non-fatal
            pass
        # Fire a push notification so the user knows their Powerwall just
        # went off-grid automatically. Best-effort — never block activation.
        try:
            from ..automations.actions import _send_expo_push

            pretty_reason = {
                "negative_price": "negative export price",
                "negative_import_price": "negative import price",
                "negative_export_earnings": "negative export earnings",
            }.get(reason, reason)
            await _send_expo_push(
                self._hass,
                "⚡ Powerwall Off-Grid",
                f"Disconnected from grid to block excess export "
                f"({pretty_reason}). SOC {soc_val:.0f}%.",
            )
        except Exception as err:
            _LOGGER.debug("Off-grid activation push failed: %s", err)
        return True

    async def release(
        self, trigger_reason: str | None = None, force: bool = False
    ) -> bool:
        """Reconnect to the grid if we own the current off-grid session.

        Safe to call when not active — returns True (nothing to do).
        The ``trigger_reason`` argument is only used for logging so an
        operator can see which restore path triggered the release.

        ``force=True`` bypasses the "not active" early-return and issues
        the real ``reconnect_grid()`` command anyway. This exists for the
        startup orphan-cleanup path: a HA reload/restart rebuilds this
        state machine fresh with ``_active=False`` even though the
        Powerwall's grid contactor may still be physically open from a
        session that predates the reload. Without ``force``, that stale
        in-memory flag would silently skip the reconnect and strand the
        house off-grid. All other callers keep the default (``force=False``)
        behavior, which is unchanged.
        """
        if not self._active and not force:
            return True

        coord = self._get_coordinator()
        if coord is None or coord.client is None:
            _LOGGER.warning(
                "Curtailment fallback: release requested but client unavailable"
            )
            return False

        try:
            ok = await coord.client.reconnect_grid()
        except PowerwallLocalError as err:
            _LOGGER.error("Curtailment fallback: reconnect_grid failed: %s", err)
            return False
        except Exception as err:
            _LOGGER.error(
                "Curtailment fallback: unexpected restore error: %s",
                err,
                exc_info=True,
            )
            return False

        if not ok:
            _LOGGER.warning(
                "Curtailment fallback: reconnect_grid command failed"
            )
            return False

        prev_reason = self._reason
        prev_started = self._started_at
        if prev_started is not None:
            self._daily_duration_s += max(0.0, time.time() - prev_started)

        self._active = False
        self._started_at = None
        self._reason = None
        session_s = int(time.time() - prev_started) if prev_started is not None else 0
        _LOGGER.info(
            "⚡ Powerwall off-grid curtailment RELEASED (was reason=%s, "
            "trigger=%s, session=%ss, daily=%ss, forced=%s)",
            prev_reason,
            trigger_reason,
            session_s,
            int(self._daily_duration_s),
            force,
        )
        try:
            await coord.async_request_refresh()
        except Exception:
            pass
        try:
            from ..automations.actions import _send_expo_push

            minutes = max(1, abs(session_s) // 60)
            await _send_expo_push(
                self._hass,
                "⚡ Powerwall Back On-Grid",
                f"Reconnected after {minutes}m off-grid curtailment.",
            )
        except Exception as err:
            _LOGGER.debug("Off-grid release push failed: %s", err)
        return True

    async def check_safety(self) -> bool:
        """Check SOC floor and daily cap during an active session.

        Call this periodically (e.g. every optimizer tick) while off-grid.
        Auto-releases and reconnects if SOC drops below floor or daily
        cap is exceeded. Returns True if still safe, False if released.
        """
        if not self._active:
            return True

        soc_ok, soc_val, floor = self._check_soc_floor()
        if not soc_ok:
            _LOGGER.warning(
                "⚡ Off-grid safety: SOC %s%% dropped below floor %s%% — "
                "emergency reconnect",
                soc_val, floor,
            )
            await self.release(trigger_reason="soc_below_floor")
            return False

        if not self._within_daily_cap():
            _LOGGER.warning(
                "⚡ Off-grid safety: daily cap %ss exceeded — "
                "emergency reconnect",
                self._daily_cap_seconds(),
            )
            await self.release(trigger_reason="daily_cap_exceeded")
            return False

        return True

    # ── Internal gates ──────────────────────────────────────────────

    def _is_enabled(self) -> bool:
        return bool(
            self._entry.options.get(
                CONF_POWERWALL_OFFGRID_AS_CURTAILMENT,
                self._entry.data.get(
                    CONF_POWERWALL_OFFGRID_AS_CURTAILMENT,
                    DEFAULT_POWERWALL_OFFGRID_AS_CURTAILMENT,
                ),
            )
        )

    def _is_paired(self) -> bool:
        return bool(self._entry.data.get(CONF_POWERWALL_LOCAL_PAIRED, False))

    def _get_current_soc(self) -> float | None:
        """Get current SOC from local coordinator or cloud sensor fallback."""
        coord = self._get_coordinator()
        if coord is not None and coord.data is not None and coord.data.soc is not None:
            return coord.data.soc
        # Fall back to cloud battery_level sensor (common when PW2
        # gateway isn't on the same LAN as HA)
        state = self._hass.states.get("sensor.power_sync_battery_level")
        if state is not None and state.state not in (None, "unknown", "unavailable"):
            try:
                return float(state.state)
            except (TypeError, ValueError):
                pass
        return None

    def _check_soc_floor(self) -> tuple[bool, float | None, int]:
        """Return (pass?, current_soc, floor)."""
        floor = int(
            self._entry.options.get(
                CONF_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC,
                self._entry.data.get(
                    CONF_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC,
                    DEFAULT_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC,
                ),
            )
        )
        soc = self._get_current_soc()
        if soc is None:
            # No telemetry available — refuse to go off-grid blindly.
            return False, None, floor
        return soc >= floor, soc, floor

    def _daily_cap_seconds(self) -> int:
        return int(
            self._entry.options.get(
                CONF_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS,
                self._entry.data.get(
                    CONF_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS,
                    DEFAULT_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS,
                ),
            )
        )

    def _current_session_seconds(self) -> float:
        if not self._active or self._started_at is None:
            return 0.0
        return max(0.0, time.time() - self._started_at)

    def _within_daily_cap(self) -> bool:
        self._maybe_reset_daily_counter()
        cap = self._daily_cap_seconds()
        total = self._daily_duration_s + self._current_session_seconds()
        return total < cap

    def _maybe_reset_daily_counter(self) -> None:
        # Daily cap rolls over at HA-local midnight, not container UTC midnight
        today = dt_util.now().date()
        if self._daily_reset_date != today:
            self._daily_duration_s = 0.0
            self._daily_reset_date = today


def get_fallback(
    hass: HomeAssistant, entry: ConfigEntry
) -> PowerwallCurtailmentFallback:
    """Fetch or construct the per-entry curtailment fallback singleton.

    Caches the instance on ``hass.data[DOMAIN][entry_id]["powerwall_local"]``
    so the runtime state (active session, daily accumulator) is shared by
    all call sites: ``apply_inverter_curtailment``, the AEMO spike manager,
    demand charge handler, and the HTTP views.
    """
    bucket = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    runtime = bucket.setdefault(
        "powerwall_local",
        {"client": None, "coordinator": None, "pairing_manager": None},
    )
    fallback = runtime.get("curtailment_fallback")
    if fallback is None:
        fallback = PowerwallCurtailmentFallback(
            hass,
            entry,
            coordinator_getter=lambda: runtime.get("coordinator"),
        )
        runtime["curtailment_fallback"] = fallback
    return fallback
