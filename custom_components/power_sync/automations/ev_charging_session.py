"""
EV Charging Session Tracking.

Tracks charging sessions with detailed energy, cost, and source breakdown.
Supports solar surplus vs grid charging attribution.
"""

import logging
import uuid
import json
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from enum import Enum
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


class ChargingSource(Enum):
    """Source of charging energy."""
    SOLAR_SURPLUS = "solar_surplus"
    GRID_OFFPEAK = "grid_offpeak"
    GRID_PEAK = "grid_peak"
    MIXED = "mixed"


@dataclass
class ChargingSegment:
    """A continuous charging segment with consistent source."""
    start_time: str  # ISO format
    end_time: Optional[str] = None
    source: str = "solar_surplus"  # ChargingSource value
    energy_kwh: float = 0.0
    avg_power_kw: float = 0.0
    avg_amps: int = 0
    solar_power_kwh: float = 0.0
    grid_power_kwh: float = 0.0
    cost_cents: float = 0.0
    savings_cents: float = 0.0  # vs grid charging

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ChargingSegment":
        """Create from dictionary."""
        return cls(**data)


@dataclass
class ChargingSession:
    """A complete EV charging session from start to stop."""
    id: str
    vehicle_id: str
    start_time: str  # ISO format
    end_time: Optional[str] = None

    # Energy tracking
    total_energy_kwh: float = 0.0
    solar_energy_kwh: float = 0.0
    grid_energy_kwh: float = 0.0
    solar_percentage: float = 0.0

    # Cost tracking
    total_cost_cents: float = 0.0
    grid_cost_avoided_cents: float = 0.0  # What it would have cost at grid rates
    export_revenue_lost_cents: float = 0.0  # FiT we didn't get
    net_savings_cents: float = 0.0

    # Vehicle state
    start_soc: Optional[int] = None
    end_soc: Optional[int] = None
    target_soc: Optional[int] = None

    # Session metadata
    mode: str = "solar_surplus"  # or "battery_target", "scheduled", "boost"
    completed: bool = False
    stopped_reason: Optional[str] = None  # "target_reached", "unplugged", "manual", "time_window"

    # Detailed segments
    segments: List[ChargingSegment] = field(default_factory=list)

    # Timestamp of last reading for elapsed time calculation
    last_reading_time: Optional[str] = None

    # Running totals for segment tracking
    _current_segment_start: Optional[str] = field(default=None, repr=False)
    _current_segment_source: Optional[str] = field(default=None, repr=False)
    _current_segment_readings: List[dict] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        data = {
            "id": self.id,
            "vehicle_id": self.vehicle_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "total_energy_kwh": self.total_energy_kwh,
            "solar_energy_kwh": self.solar_energy_kwh,
            "grid_energy_kwh": self.grid_energy_kwh,
            "solar_percentage": self.solar_percentage,
            "total_cost_cents": self.total_cost_cents,
            "grid_cost_avoided_cents": self.grid_cost_avoided_cents,
            "export_revenue_lost_cents": self.export_revenue_lost_cents,
            "net_savings_cents": self.net_savings_cents,
            "start_soc": self.start_soc,
            "end_soc": self.end_soc,
            "target_soc": self.target_soc,
            "mode": self.mode,
            "completed": self.completed,
            "stopped_reason": self.stopped_reason,
            "last_reading_time": self.last_reading_time,
            "segments": [s.to_dict() if isinstance(s, ChargingSegment) else s for s in self.segments],
        }
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ChargingSession":
        """Create from dictionary."""
        segments = [
            ChargingSegment.from_dict(s) if isinstance(s, dict) else s
            for s in data.get("segments", [])
        ]
        return cls(
            id=data["id"],
            vehicle_id=data["vehicle_id"],
            start_time=data["start_time"],
            end_time=data.get("end_time"),
            total_energy_kwh=data.get("total_energy_kwh", 0.0),
            solar_energy_kwh=data.get("solar_energy_kwh", 0.0),
            grid_energy_kwh=data.get("grid_energy_kwh", 0.0),
            solar_percentage=data.get("solar_percentage", 0.0),
            total_cost_cents=data.get("total_cost_cents", 0.0),
            grid_cost_avoided_cents=data.get("grid_cost_avoided_cents", 0.0),
            export_revenue_lost_cents=data.get("export_revenue_lost_cents", 0.0),
            net_savings_cents=data.get("net_savings_cents", 0.0),
            start_soc=data.get("start_soc"),
            end_soc=data.get("end_soc"),
            target_soc=data.get("target_soc"),
            mode=data.get("mode", "solar_surplus"),
            completed=data.get("completed", False),
            stopped_reason=data.get("stopped_reason"),
            last_reading_time=data.get("last_reading_time"),
            segments=segments,
        )

    @property
    def duration_minutes(self) -> float:
        """Calculate session duration in minutes."""
        try:
            if not self.end_time:
                end = datetime.now()
            else:
                end = datetime.fromisoformat(self.end_time.replace("Z", "+00:00"))
            start = datetime.fromisoformat(self.start_time.replace("Z", "+00:00"))
            return (end - start).total_seconds() / 60
        except (ValueError, TypeError):
            return 0.0

    def start_segment(self, source: str) -> None:
        """Start a new charging segment."""
        # Close any existing segment first
        if self._current_segment_start:
            self.end_segment()

        self._current_segment_start = datetime.now().isoformat()
        self._current_segment_source = source
        self._current_segment_readings = []

    def add_reading(
        self,
        power_kw: float,
        amps: int,
        is_solar: bool,
        import_price_cents: float,
        export_price_cents: float,
    ) -> None:
        """Add a power reading to the current segment.

        Args:
            power_kw: Current charging power in kW
            amps: Current charging amps
            is_solar: True if this power is from solar surplus
            import_price_cents: Grid import price in cents/kWh
            export_price_cents: Feed-in tariff in cents/kWh
        """
        # Clamp negative power values (sensor glitches)
        power_kw = max(0.0, power_kw)

        if not self._current_segment_start:
            # Auto-start a segment
            source = ChargingSource.SOLAR_SURPLUS.value if is_solar else ChargingSource.GRID_PEAK.value
            self.start_segment(source)

        # Calculate elapsed time from last reading
        now = datetime.now()
        if self.last_reading_time:
            try:
                last = datetime.fromisoformat(self.last_reading_time)
                interval_seconds = (now - last).total_seconds()
                # Cap at 120s to avoid huge energy spikes from delayed readings
                interval_seconds = min(interval_seconds, 120.0)
            except (ValueError, TypeError):
                interval_seconds = 30.0
        else:
            # First reading — default to 30s
            interval_seconds = 30.0
        self.last_reading_time = now.isoformat()

        # Calculate energy for this interval
        energy_kwh = (power_kw * interval_seconds) / 3600

        # Determine cost and savings
        if is_solar:
            # Using solar surplus - cost is the FiT we're not getting
            cost_cents = energy_kwh * export_price_cents
            # Savings is what we would have paid for grid
            savings_cents = energy_kwh * import_price_cents
            solar_kwh = energy_kwh
            grid_kwh = 0.0
        else:
            # Using grid power
            cost_cents = energy_kwh * import_price_cents
            savings_cents = 0.0
            solar_kwh = 0.0
            grid_kwh = energy_kwh

        # Store reading for segment aggregation
        self._current_segment_readings.append({
            "timestamp": datetime.now().isoformat(),
            "power_kw": power_kw,
            "amps": amps,
            "energy_kwh": energy_kwh,
            "solar_kwh": solar_kwh,
            "grid_kwh": grid_kwh,
            "cost_cents": cost_cents,
            "savings_cents": savings_cents,
        })

        # Update session totals
        self.total_energy_kwh += energy_kwh
        self.solar_energy_kwh += solar_kwh
        self.grid_energy_kwh += grid_kwh
        self.total_cost_cents += cost_cents

        # Cost avoided is grid price for solar energy used
        if is_solar:
            self.grid_cost_avoided_cents += energy_kwh * import_price_cents
            self.export_revenue_lost_cents += cost_cents

        # Update solar percentage
        if self.total_energy_kwh > 0:
            self.solar_percentage = (self.solar_energy_kwh / self.total_energy_kwh) * 100

        # Calculate net savings
        self.net_savings_cents = self.grid_cost_avoided_cents - self.export_revenue_lost_cents

    def end_segment(self) -> Optional[ChargingSegment]:
        """End the current segment and return it."""
        if not self._current_segment_start or not self._current_segment_readings:
            return None

        readings = self._current_segment_readings

        # Aggregate readings into segment
        segment = ChargingSegment(
            start_time=self._current_segment_start,
            end_time=datetime.now().isoformat(),
            source=self._current_segment_source or ChargingSource.MIXED.value,
            energy_kwh=sum(r["energy_kwh"] for r in readings),
            avg_power_kw=statistics.mean(r["power_kw"] for r in readings) if readings else 0,
            avg_amps=int(statistics.mean(r["amps"] for r in readings)) if readings else 0,
            solar_power_kwh=sum(r["solar_kwh"] for r in readings),
            grid_power_kwh=sum(r["grid_kwh"] for r in readings),
            cost_cents=sum(r["cost_cents"] for r in readings),
            savings_cents=sum(r["savings_cents"] for r in readings),
        )

        self.segments.append(segment)

        # Reset segment tracking
        self._current_segment_start = None
        self._current_segment_source = None
        self._current_segment_readings = []

        return segment


class ChargingSessionManager:
    """Manages charging sessions and calculates statistics."""

    def __init__(self, hass, storage_path: Optional[Path] = None):
        """Initialize the session manager.

        Args:
            hass: Home Assistant instance
            storage_path: Path to store session data (default: HA config dir)
        """
        self.hass = hass
        self.active_sessions: Dict[str, ChargingSession] = {}
        self._storage_path = storage_path

        # Load existing sessions on init
        self._sessions_cache: List[ChargingSession] = []
        self._cache_loaded = False

    @property
    def storage_path(self) -> Path:
        """Get the storage path for session data."""
        if self._storage_path:
            return self._storage_path
        # Default to HA config directory
        return Path(self.hass.config.config_dir) / "power_sync_ev_sessions.json"

    async def _load_sessions(self) -> None:
        """Load sessions from storage."""
        if self._cache_loaded:
            return

        try:
            if self.storage_path.exists():
                def _read_file():
                    with open(self.storage_path, "r") as f:
                        return json.load(f)

                data = await self.hass.async_add_executor_job(_read_file)
                self._sessions_cache = [
                    ChargingSession.from_dict(s) for s in data.get("sessions", [])
                ]
                _LOGGER.info(f"Loaded {len(self._sessions_cache)} charging sessions from storage")
            self._cache_loaded = True
        except Exception as e:
            _LOGGER.error(f"Failed to load sessions from storage: {e}")
            self._sessions_cache = []
            self._cache_loaded = True

    async def _save_sessions(self) -> None:
        """Save sessions to storage."""
        try:
            # Keep last 365 days of sessions
            cutoff = datetime.now() - timedelta(days=365)
            cutoff_str = cutoff.isoformat()

            sessions_to_save = [
                s for s in self._sessions_cache
                if s.start_time >= cutoff_str
            ]

            def _write_file():
                data = {"sessions": [s.to_dict() for s in sessions_to_save]}
                with open(self.storage_path, "w") as f:
                    json.dump(data, f, indent=2)

            await self.hass.async_add_executor_job(_write_file)
            _LOGGER.debug(f"Saved {len(sessions_to_save)} charging sessions to storage")
        except Exception as e:
            _LOGGER.error(f"Failed to save sessions to storage: {e}")

    async def start_session(
        self,
        vehicle_id: str,
        mode: str,
        start_soc: Optional[int] = None,
        target_soc: Optional[int] = None,
    ) -> ChargingSession:
        """Start a new charging session.

        Args:
            vehicle_id: Vehicle identifier
            mode: Charging mode (solar_surplus, battery_target, scheduled, boost)
            start_soc: Starting state of charge (%)
            target_soc: Target state of charge (%)

        Returns:
            New ChargingSession instance
        """
        await self._load_sessions()

        # End any existing session for this vehicle
        if vehicle_id in self.active_sessions:
            await self.end_session(vehicle_id, "new_session_started")

        session = ChargingSession(
            id=str(uuid.uuid4()),
            vehicle_id=vehicle_id,
            start_time=datetime.now().isoformat(),
            mode=mode,
            start_soc=start_soc,
            target_soc=target_soc,
        )

        self.active_sessions[vehicle_id] = session
        _LOGGER.info(f"Started charging session {session.id} for {vehicle_id} (mode={mode})")

        return session

    async def update_session(
        self,
        vehicle_id: str,
        power_kw: float,
        amps: int,
        is_solar: bool,
        import_price_cents: float = 30.0,
        export_price_cents: float = 8.0,
        battery_soc: Optional[int] = None,
    ) -> Optional[ChargingSession]:
        """Update session with new power reading.

        Called periodically (every 30s) during charging.

        Args:
            vehicle_id: Vehicle identifier
            power_kw: Current charging power in kW
            amps: Current charging amps
            is_solar: True if power is from solar surplus
            import_price_cents: Grid import price (cents/kWh)
            export_price_cents: Feed-in tariff (cents/kWh)
            battery_soc: Current battery SoC (if available)

        Returns:
            Updated session or None if no active session
        """
        session = self.active_sessions.get(vehicle_id)
        if not session:
            return None

        # Determine source from is_solar flag
        source = ChargingSource.SOLAR_SURPLUS.value if is_solar else ChargingSource.GRID_PEAK.value

        # Check if source changed - if so, end current segment and start new one
        if session._current_segment_source and session._current_segment_source != source:
            session.end_segment()
            session.start_segment(source)
        elif not session._current_segment_start:
            session.start_segment(source)

        # Add the reading
        session.add_reading(
            power_kw=power_kw,
            amps=amps,
            is_solar=is_solar,
            import_price_cents=import_price_cents,
            export_price_cents=export_price_cents,
        )

        return session

    async def end_session(
        self,
        vehicle_id: str,
        reason: str,
        end_soc: Optional[int] = None,
    ) -> Optional[ChargingSession]:
        """End a charging session and persist to storage.

        Args:
            vehicle_id: Vehicle identifier
            reason: Why the session ended
            end_soc: Ending state of charge (%)

        Returns:
            Completed session or None if no active session
        """
        session = self.active_sessions.pop(vehicle_id, None)
        if not session:
            return None

        # End any active segment
        session.end_segment()

        # Finalize session
        session.end_time = datetime.now().isoformat()
        session.completed = True
        session.stopped_reason = reason
        session.end_soc = end_soc

        # Add to cache and save
        self._sessions_cache.append(session)
        await self._save_sessions()

        _LOGGER.info(
            f"Ended charging session {session.id}: "
            f"{session.total_energy_kwh:.2f} kWh "
            f"({session.solar_percentage:.0f}% solar), "
            f"cost=${session.total_cost_cents/100:.2f}, "
            f"saved=${session.net_savings_cents/100:.2f}"
        )

        return session

    async def get_active_session(self, vehicle_id: str) -> Optional[ChargingSession]:
        """Get the active session for a vehicle."""
        return self.active_sessions.get(vehicle_id)

    async def get_session_history(
        self,
        vehicle_id: Optional[str] = None,
        days: int = 30,
        limit: int = 100,
    ) -> List[ChargingSession]:
        """Get historical charging sessions.

        Args:
            vehicle_id: Filter by vehicle (None for all)
            days: Number of days to look back
            limit: Maximum sessions to return

        Returns:
            List of completed sessions, newest first
        """
        await self._load_sessions()

        cutoff = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff.isoformat()

        sessions = [
            s for s in self._sessions_cache
            if s.completed and s.start_time >= cutoff_str
            and (vehicle_id is None or s.vehicle_id == vehicle_id)
        ]

        # Sort by start time, newest first
        sessions.sort(key=lambda s: s.start_time, reverse=True)

        return sessions[:limit]

    async def get_statistics(
        self,
        vehicle_id: Optional[str] = None,
        days: int = 30,
    ) -> dict:
        """Calculate charging statistics.

        Args:
            vehicle_id: Filter by vehicle (None for all)
            days: Number of days to analyze

        Returns:
            Dictionary with statistics
        """
        sessions = await self.get_session_history(vehicle_id, days, limit=10000)

        if not sessions:
            return {
                "period_days": days,
                "total_sessions": 0,
                "total_energy_kwh": 0,
                "solar_energy_kwh": 0,
                "grid_energy_kwh": 0,
                "solar_percentage": 0,
                "total_cost_dollars": 0,
                "total_savings_dollars": 0,
                "avg_cost_per_kwh_cents": 0,
                "avg_session_duration_minutes": 0,
                "avg_session_energy_kwh": 0,
                "by_vehicle": {},
                "by_day": [],
            }

        # Calculate totals
        total_energy = sum(s.total_energy_kwh for s in sessions)
        solar_energy = sum(s.solar_energy_kwh for s in sessions)
        grid_energy = sum(s.grid_energy_kwh for s in sessions)
        total_cost = sum(s.total_cost_cents for s in sessions)
        total_savings = sum(s.net_savings_cents for s in sessions)

        # Calculate per-vehicle stats
        by_vehicle: Dict[str, dict] = {}
        for session in sessions:
            vid = session.vehicle_id
            if vid not in by_vehicle:
                by_vehicle[vid] = {
                    "sessions": 0,
                    "energy_kwh": 0,
                    "solar_energy_kwh": 0,
                    "savings_dollars": 0,
                }
            by_vehicle[vid]["sessions"] += 1
            by_vehicle[vid]["energy_kwh"] += session.total_energy_kwh
            by_vehicle[vid]["solar_energy_kwh"] += session.solar_energy_kwh
            by_vehicle[vid]["savings_dollars"] += session.net_savings_cents / 100

        # Add solar percentage to each vehicle
        for vid, stats in by_vehicle.items():
            if stats["energy_kwh"] > 0:
                stats["solar_percentage"] = (stats["solar_energy_kwh"] / stats["energy_kwh"]) * 100
            else:
                stats["solar_percentage"] = 0

        # Calculate daily totals
        by_day: Dict[str, dict] = {}
        for session in sessions:
            day = session.start_time[:10]  # YYYY-MM-DD
            if day not in by_day:
                by_day[day] = {
                    "date": day,
                    "energy_kwh": 0,
                    "solar_kwh": 0,
                    "cost_cents": 0,
                }
            by_day[day]["energy_kwh"] += session.total_energy_kwh
            by_day[day]["solar_kwh"] += session.solar_energy_kwh
            by_day[day]["cost_cents"] += session.total_cost_cents

        # Sort days chronologically
        daily_list = sorted(by_day.values(), key=lambda d: d["date"])

        # Calculate averages
        durations = [s.duration_minutes for s in sessions]
        energies = [s.total_energy_kwh for s in sessions]

        return {
            "period_days": days,
            "total_sessions": len(sessions),
            "total_energy_kwh": round(total_energy, 2),
            "solar_energy_kwh": round(solar_energy, 2),
            "grid_energy_kwh": round(grid_energy, 2),
            "solar_percentage": round((solar_energy / total_energy * 100) if total_energy > 0 else 0, 1),
            "total_cost_dollars": round(total_cost / 100, 2),
            "total_savings_dollars": round(total_savings / 100, 2),
            "avg_cost_per_kwh_cents": round((total_cost / total_energy) if total_energy > 0 else 0, 1),
            "avg_session_duration_minutes": round(statistics.mean(durations) if durations else 0, 1),
            "avg_session_energy_kwh": round(statistics.mean(energies) if energies else 0, 2),
            "by_vehicle": by_vehicle,
            "by_day": daily_list,
            "environmental": {
                "co2_avoided_kg": round(solar_energy * 0.5, 1),  # ~0.5kg CO2 per kWh from grid
                "trees_equivalent": round(solar_energy * 0.5 / 21, 1),  # ~21kg CO2 per tree per year
            },
            "cost_comparison": {
                "equivalent_petrol_dollars": round(self._calculate_petrol_equivalent(total_energy), 2),
            },
        }

    def _calculate_petrol_equivalent(self, energy_kwh: float) -> float:
        """Calculate equivalent petrol cost for same distance.

        Assumes:
        - 6 km/kWh for EV
        - 12 km/L for petrol car
        - $2.00/L petrol price
        """
        ev_km = energy_kwh * 6
        petrol_litres = ev_km / 12
        return petrol_litres * 2.0


# Global session manager instance (initialized by __init__.py)
_session_manager: Optional[ChargingSessionManager] = None


def get_session_manager() -> Optional[ChargingSessionManager]:
    """Get the global session manager instance."""
    return _session_manager


def set_session_manager(manager: ChargingSessionManager) -> None:
    """Set the global session manager instance."""
    global _session_manager
    _session_manager = manager
