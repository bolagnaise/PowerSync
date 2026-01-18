"""
Solcast Solar Forecasting API Integration.

Provides solar production forecasts for better curtailment decisions.
Uses the Solcast Rooftop API for PV power forecasts.

API Documentation: https://docs.solcast.com.au/
"""

import logging
import requests
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from app import db
from app.models import User, SolcastForecast
from app.utils import decrypt_token

_LOGGER = logging.getLogger(__name__)

# Solcast API configuration
SOLCAST_API_BASE_URL = "https://api.solcast.com.au"
SOLCAST_TIMEOUT = 30  # seconds

# Cache duration - forecasts are valid for several hours
# Hobbyist tier: 10 API calls/day, so we cache for at least 2.4 hours
FORECAST_CACHE_HOURS = 3


class SolcastService:
    """Service for interacting with Solcast solar forecasting API."""

    def __init__(self, user: User):
        """Initialize with user's Solcast credentials.

        Args:
            user: User with Solcast API key and resource ID configured
        """
        self.user = user
        self._api_key: Optional[str] = None

    @property
    def api_key(self) -> Optional[str]:
        """Get decrypted Solcast API key."""
        if self._api_key is None and self.user.solcast_api_key_encrypted:
            try:
                self._api_key = decrypt_token(self.user.solcast_api_key_encrypted)
            except Exception as e:
                _LOGGER.error(f"Failed to decrypt Solcast API key: {e}")
        return self._api_key

    @property
    def resource_id(self) -> Optional[str]:
        """Get Solcast rooftop site resource ID."""
        return self.user.solcast_resource_id

    @property
    def is_configured(self) -> bool:
        """Check if Solcast is properly configured."""
        return bool(
            self.user.solcast_enabled
            and self.api_key
            and self.resource_id
        )

    def _get_headers(self) -> Dict[str, str]:
        """Get API request headers with authentication."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make authenticated request to Solcast API.

        Args:
            endpoint: API endpoint path
            params: Query parameters

        Returns:
            JSON response or None on error
        """
        if not self.api_key:
            _LOGGER.error("Solcast API key not configured")
            return None

        url = f"{SOLCAST_API_BASE_URL}{endpoint}"

        try:
            response = requests.get(
                url,
                headers=self._get_headers(),
                params=params,
                timeout=SOLCAST_TIMEOUT
            )

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 401:
                _LOGGER.error("Solcast API authentication failed - check API key")
            elif response.status_code == 429:
                _LOGGER.warning("Solcast API rate limit exceeded")
            else:
                _LOGGER.error(f"Solcast API error: {response.status_code} - {response.text[:200]}")

        except requests.RequestException as e:
            _LOGGER.error(f"Solcast API request failed: {e}")

        return None

    def fetch_forecast(self, hours: int = 48) -> Optional[List[Dict]]:
        """Fetch solar production forecast from Solcast API.

        Args:
            hours: Number of hours to forecast (default 48, max 168)

        Returns:
            List of forecast periods with pv_estimate values, or None on error
        """
        if not self.is_configured:
            _LOGGER.debug("Solcast not configured, skipping forecast fetch")
            return None

        endpoint = f"/rooftop_sites/{self.resource_id}/forecasts"
        params = {
            "hours": min(hours, 168),
            "format": "json",
        }

        data = self._make_request(endpoint, params)
        if not data:
            return None

        forecasts = data.get("forecasts", [])
        _LOGGER.info(f"Fetched {len(forecasts)} Solcast forecast periods for next {hours} hours")

        return forecasts

    def fetch_estimated_actuals(self, hours: int = 24) -> Optional[List[Dict]]:
        """Fetch estimated actuals (recent history) from Solcast API.

        Args:
            hours: Number of hours of history (default 24, max 168)

        Returns:
            List of estimated actual periods, or None on error
        """
        if not self.is_configured:
            return None

        endpoint = f"/rooftop_sites/{self.resource_id}/estimated_actuals"
        params = {
            "hours": min(hours, 168),
            "format": "json",
        }

        data = self._make_request(endpoint, params)
        if not data:
            return None

        actuals = data.get("estimated_actuals", [])
        _LOGGER.info(f"Fetched {len(actuals)} Solcast estimated actuals for past {hours} hours")

        return actuals

    def update_forecast_cache(self) -> bool:
        """Update cached forecasts from Solcast API.

        Returns:
            True if cache was updated successfully
        """
        forecasts = self.fetch_forecast(hours=48)
        if not forecasts:
            return False

        try:
            # Clear old forecasts for this user
            SolcastForecast.query.filter_by(user_id=self.user.id).delete()

            # Insert new forecasts
            now = datetime.utcnow()
            for forecast in forecasts:
                period_end_str = forecast.get("period_end")
                if not period_end_str:
                    continue

                # Parse ISO 8601 datetime (e.g., "2024-01-18T10:00:00.0000000Z")
                period_end = datetime.fromisoformat(period_end_str.replace("Z", "+00:00"))
                # Convert to naive datetime for SQLite compatibility
                period_end = period_end.replace(tzinfo=None)

                db_forecast = SolcastForecast(
                    user_id=self.user.id,
                    period_end=period_end,
                    pv_estimate=forecast.get("pv_estimate"),
                    pv_estimate10=forecast.get("pv_estimate10"),
                    pv_estimate90=forecast.get("pv_estimate90"),
                    created_at=now,
                    updated_at=now,
                )
                db.session.add(db_forecast)

            db.session.commit()
            _LOGGER.info(f"Updated Solcast forecast cache with {len(forecasts)} periods")
            return True

        except Exception as e:
            _LOGGER.error(f"Failed to update Solcast forecast cache: {e}")
            db.session.rollback()
            return False

    def get_cached_forecast(self, hours_ahead: int = 24) -> List[SolcastForecast]:
        """Get cached forecasts from database.

        Args:
            hours_ahead: How many hours ahead to retrieve

        Returns:
            List of SolcastForecast objects
        """
        now = datetime.utcnow()
        end_time = now + timedelta(hours=hours_ahead)

        return SolcastForecast.query.filter(
            SolcastForecast.user_id == self.user.id,
            SolcastForecast.period_end >= now,
            SolcastForecast.period_end <= end_time
        ).order_by(SolcastForecast.period_end).all()

    def get_forecast_for_period(self, target_time: datetime) -> Optional[SolcastForecast]:
        """Get forecast for a specific time period.

        Args:
            target_time: The time to get forecast for

        Returns:
            SolcastForecast for the period containing target_time, or None
        """
        # Find the forecast period that contains the target time
        # Solcast uses period_end, so we find the first period that ends after target_time
        return SolcastForecast.query.filter(
            SolcastForecast.user_id == self.user.id,
            SolcastForecast.period_end > target_time
        ).order_by(SolcastForecast.period_end).first()

    def should_refresh_cache(self) -> bool:
        """Check if forecast cache should be refreshed.

        Returns:
            True if cache is stale or empty
        """
        # Check most recent forecast update time
        latest = SolcastForecast.query.filter_by(
            user_id=self.user.id
        ).order_by(SolcastForecast.updated_at.desc()).first()

        if not latest:
            return True

        # Check if cache is older than threshold
        age = datetime.utcnow() - latest.updated_at
        return age > timedelta(hours=FORECAST_CACHE_HOURS)

    def get_expected_production(self, hours_ahead: int = 1) -> Optional[float]:
        """Get expected solar production for the next N hours.

        Args:
            hours_ahead: Hours to look ahead (default 1)

        Returns:
            Expected production in kW (50th percentile), or None if unavailable
        """
        forecasts = self.get_cached_forecast(hours_ahead)
        if not forecasts:
            return None

        # Average the pv_estimate values
        estimates = [f.pv_estimate for f in forecasts if f.pv_estimate is not None]
        if not estimates:
            return None

        return sum(estimates) / len(estimates)

    def get_production_summary(self, hours_ahead: int = 24) -> Dict[str, Any]:
        """Get summary of expected solar production.

        Args:
            hours_ahead: Hours to summarize

        Returns:
            Dictionary with production summary
        """
        forecasts = self.get_cached_forecast(hours_ahead)

        if not forecasts:
            return {
                "available": False,
                "message": "No forecast data available",
            }

        estimates = [f.pv_estimate for f in forecasts if f.pv_estimate is not None]
        estimates_low = [f.pv_estimate10 for f in forecasts if f.pv_estimate10 is not None]
        estimates_high = [f.pv_estimate90 for f in forecasts if f.pv_estimate90 is not None]

        # Calculate period duration (typically 30 minutes)
        period_hours = 0.5  # 30-minute periods

        return {
            "available": True,
            "hours_ahead": hours_ahead,
            "periods": len(forecasts),
            "current_estimate_kw": estimates[0] if estimates else None,
            "peak_estimate_kw": max(estimates) if estimates else None,
            "average_estimate_kw": sum(estimates) / len(estimates) if estimates else None,
            "total_energy_kwh": sum(estimates) * period_hours if estimates else None,
            "total_energy_kwh_low": sum(estimates_low) * period_hours if estimates_low else None,
            "total_energy_kwh_high": sum(estimates_high) * period_hours if estimates_high else None,
            "last_updated": forecasts[0].updated_at.isoformat() if forecasts else None,
        }


def get_solcast_service(user: User) -> Optional[SolcastService]:
    """Get Solcast service for a user if configured.

    Args:
        user: User to get service for

    Returns:
        SolcastService if configured, None otherwise
    """
    if not user.solcast_enabled:
        return None

    service = SolcastService(user)
    if not service.is_configured:
        return None

    return service


def refresh_user_forecast(user: User) -> bool:
    """Refresh Solcast forecast for a user.

    Args:
        user: User to refresh forecast for

    Returns:
        True if refresh was successful
    """
    service = get_solcast_service(user)
    if not service:
        return False

    if service.should_refresh_cache():
        return service.update_forecast_cache()

    return True  # Cache is fresh


def get_user_production_forecast(user: User, hours_ahead: int = 24) -> Dict[str, Any]:
    """Get production forecast summary for a user.

    Args:
        user: User to get forecast for
        hours_ahead: Hours to forecast

    Returns:
        Production summary dictionary
    """
    service = get_solcast_service(user)
    if not service:
        return {
            "available": False,
            "message": "Solcast not configured",
        }

    # Refresh cache if needed
    if service.should_refresh_cache():
        service.update_forecast_cache()

    return service.get_production_summary(hours_ahead)
