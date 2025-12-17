# app/api_clients.py
"""API clients for Amber Electric and Tesla"""
import requests
import logging
from datetime import datetime, timedelta
from app.utils import decrypt_token
import time
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# Version and User-Agent for API identification
TESLA_SYNC_VERSION = "2.1.0"
TESLA_SYNC_USER_AGENT = f"TeslaSync/{TESLA_SYNC_VERSION}"


class TeslaAPIClientBase(ABC):
    """Abstract base class for Tesla API clients (Fleet API, Teslemetry, etc.)"""

    @abstractmethod
    def test_connection(self):
        """Test the API connection"""
        pass

    @abstractmethod
    def get_energy_sites(self):
        """Get all energy sites (Powerwalls, Solar)"""
        pass

    @abstractmethod
    def get_site_status(self, site_id):
        """Get status of a specific energy site"""
        pass

    @abstractmethod
    def get_site_info(self, site_id):
        """Get detailed information about a site"""
        pass

    @abstractmethod
    def set_operation_mode(self, site_id, mode):
        """Set Powerwall operation mode (self_consumption, autonomous, backup)"""
        pass

    @abstractmethod
    def set_backup_reserve(self, site_id, backup_reserve_percent):
        """Set backup reserve percentage"""
        pass

    @abstractmethod
    def set_time_based_control_settings(self, site_id, tou_settings):
        """Set TOU (Time of Use) tariff settings"""
        pass

    @abstractmethod
    def set_grid_export_rule(self, site_id, export_rule):
        """Set grid export rule (never, pv_only, battery_ok)"""
        pass


class AmberAPIClient:
    """Client for Amber Electric API"""

    BASE_URL = "https://api.amber.com.au/v1"

    def __init__(self, api_token, site_id=None):
        self.api_token = api_token
        self.site_id = site_id  # User's selected site ID
        self.base_url = self.BASE_URL
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
        logger.info(f"AmberAPIClient initialized (site_id: {site_id or 'not set'})")

    def test_connection(self):
        """Test the API connection"""
        try:
            logger.info("Testing Amber API connection")
            response = requests.get(
                f"{self.base_url}/sites",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            logger.info(f"Amber API connection successful - Status: {response.status_code}")
            return True, "Connected"
        except requests.exceptions.RequestException as e:
            logger.error(f"Amber API connection failed: {e}")
            return False, str(e)

    def get_current_prices(self, site_id=None):
        """Get current electricity prices via REST API"""
        try:
            # Use provided site_id, or client's stored site_id, or fallback to first site
            if not site_id:
                site_id = self.site_id
            if not site_id:
                sites = self.get_sites()
                if sites:
                    site_id = sites[0]['id']
                    logger.warning(f"No site_id configured, falling back to first site: {site_id}")
                else:
                    logger.error("No Amber sites found")
                    return None

            logger.info(f"Fetching current prices for site: {site_id}")
            response = requests.get(
                f"{self.base_url}/sites/{site_id}/prices/current",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully fetched current prices: {len(data)} channels")
            logger.debug(f"Price data: {data}")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching current prices: {e}")
            return None

    def get_live_prices(self, site_id=None, ws_client=None):
        """
        Get current electricity prices with WebSocket-first approach.

        Checks WebSocket cache first for real-time prices with retry logic,
        falls back to REST API only as last resort if WebSocket unavailable.

        Args:
            site_id: Site ID (defaults to first site)
            ws_client: AmberWebSocketClient instance (optional)

        Returns:
            List of price data (same format as get_current_prices)
        """
        import time

        # Try WebSocket first if client provided
        if ws_client:
            try:
                # Retry logic: Try for 10 seconds with 2-second intervals (5 attempts)
                max_age_seconds = 60  # Reduced from 360s to 60s for fresher data
                retry_attempts = 5
                retry_interval = 2  # seconds

                for attempt in range(retry_attempts):
                    ws_prices = ws_client.get_latest_prices(max_age_seconds=max_age_seconds)

                    if ws_prices:
                        # Get health status to log data age
                        health = ws_client.get_health_status()
                        age = health.get('age_seconds', 'unknown')
                        logger.info(f"✓ Using WebSocket prices (age: {age}s, attempt: {attempt + 1}/{retry_attempts})")
                        return ws_prices

                    # If not last attempt, wait before retry
                    if attempt < retry_attempts - 1:
                        logger.debug(f"WebSocket data unavailable/stale, retrying in {retry_interval}s (attempt {attempt + 1}/{retry_attempts})")
                        time.sleep(retry_interval)

                # All retries exhausted
                logger.warning(f"WebSocket prices unavailable after {retry_attempts} attempts ({max_age_seconds}s staleness threshold), falling back to REST API")

            except Exception as e:
                logger.warning(f"Error getting WebSocket prices: {e}, falling back to REST API")

        # Fall back to REST API (only if WebSocket unavailable or failed)
        logger.info("⚠ Using REST API for current prices (WebSocket unavailable)")
        return self.get_current_prices(site_id=site_id)

    def get_sites(self):
        """Get all sites associated with the account"""
        try:
            logger.info("Fetching Amber sites")
            response = requests.get(
                f"{self.base_url}/sites",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            sites = response.json()
            logger.info(f"Found {len(sites)} Amber sites")
            return sites
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching sites: {e}")
            return []

    def get_price_forecast(self, site_id=None, start_date=None, end_date=None, next_hours=24, resolution=None):
        """
        Get price forecast for a site

        Args:
            site_id: Site ID (uses client's stored site_id if not provided)
            start_date: Start date (defaults to now)
            end_date: End date (defaults to start + next_hours)
            next_hours: Hours to fetch if end_date not specified
            resolution: Interval resolution in minutes (5 or 30, defaults to billing interval)
        """
        try:
            # Use provided site_id, or client's stored site_id, or fallback to first site
            if not site_id:
                site_id = self.site_id
            if not site_id:
                sites = self.get_sites()
                if sites:
                    site_id = sites[0]['id']
                    logger.warning(f"No site_id configured, falling back to first site: {site_id}")
                else:
                    logger.error("No Amber sites found")
                    return None

            if not start_date:
                start_date = datetime.utcnow()
            if not end_date:
                end_date = start_date + timedelta(hours=next_hours)

            res_str = f" at {resolution}min resolution" if resolution else ""
            logger.info(f"Fetching {next_hours}h price forecast{res_str} for site {site_id}")
            params = {
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat()
            }

            # Add resolution parameter if specified (5 or 30 minutes)
            if resolution:
                params["resolution"] = resolution

            response = requests.get(
                f"{self.base_url}/sites/{site_id}/prices",
                headers=self.headers,
                params=params,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully fetched forecast: {len(data)} price points")
            logger.debug(f"Forecast data sample: {data[:2] if len(data) > 0 else 'None'}")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching price forecast: {e}")
            return None

    def get_usage(self, site_id=None, start_date=None, end_date=None):
        """
        Get historical usage data for a site

        Args:
            site_id: Site ID (uses client's stored site_id if not provided)
            start_date: Start date (defaults to 7 days ago)
            end_date: End date (defaults to now)
        """
        try:
            # Use provided site_id, or client's stored site_id, or fallback to first site
            if not site_id:
                site_id = self.site_id
            if not site_id:
                sites = self.get_sites()
                if sites:
                    site_id = sites[0]['id']
                    logger.warning(f"No site_id configured, falling back to first site: {site_id}")
                else:
                    logger.error("No Amber sites found")
                    return None

            if not start_date:
                start_date = datetime.utcnow() - timedelta(days=7)
            if not end_date:
                end_date = datetime.utcnow()

            logger.info(f"Fetching usage data for site {site_id}")
            params = {
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat()
            }

            response = requests.get(
                f"{self.base_url}/sites/{site_id}/usage",
                headers=self.headers,
                params=params,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully fetched usage data: {len(data)} data points")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching usage data: {e}")
            return None

    def raw_api_call(self, endpoint, method='GET', params=None, json_data=None):
        """
        Make a raw API call to any Amber endpoint

        Args:
            endpoint: API endpoint path (e.g., '/sites' or '/sites/{site_id}/prices')
            method: HTTP method (GET, POST, etc.)
            params: Query parameters
            json_data: JSON body for POST/PUT requests

        Returns:
            tuple: (success: bool, data: dict or None, status_code: int)
        """
        try:
            url = f"{self.base_url}{endpoint}"
            logger.info(f"Making {method} request to {url}")

            response = requests.request(
                method=method,
                url=url,
                headers=self.headers,
                params=params,
                json=json_data,
                timeout=10
            )

            # Return both success status and data
            success = response.status_code < 400
            data = None

            try:
                data = response.json()
            except:
                data = {"raw_text": response.text}

            logger.info(f"API call completed with status {response.status_code}")
            return success, data, response.status_code

        except requests.exceptions.RequestException as e:
            logger.error(f"Error making API call to {endpoint}: {e}")
            return False, {"error": str(e)}, 0


class FleetAPIClient(TeslaAPIClientBase):
    """Client for Tesla Fleet API (direct connection)"""

    BASE_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"
    AUTH_URL = "https://auth.tesla.com/oauth2/v3"
    TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"

    def __init__(self, access_token, refresh_token=None, client_id=None, client_secret=None):
        """
        Initialize Fleet API client

        Args:
            access_token: OAuth access token
            refresh_token: OAuth refresh token (for automatic token refresh)
            client_id: Tesla app client ID (required for token refresh)
            client_secret: Tesla app client secret (required for token refresh)
        """
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = self.BASE_URL
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": TESLA_SYNC_USER_AGENT,
        }
        logger.info("FleetAPIClient initialized (direct Tesla Fleet API)")

    def refresh_access_token(self):
        """
        Refresh the OAuth access token using refresh token

        Returns:
            dict: New token data with access_token and refresh_token
        """
        if not self.refresh_token:
            raise ValueError("No refresh token available for token refresh")

        if not self.client_id:
            raise ValueError("Client ID required for token refresh")

        try:
            logger.info("Refreshing Fleet API access token")
            response = requests.post(
                self.TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "refresh_token": self.refresh_token
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            # Update tokens
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token", self.refresh_token)  # New refresh token if provided
            self.headers["Authorization"] = f"Bearer {self.access_token}"

            logger.info("Successfully refreshed Fleet API access token")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error refreshing access token: {e}")
            raise

    def test_connection(self):
        """Test the API connection"""
        try:
            logger.info("Testing Fleet API connection")
            response = requests.get(
                f"{self.base_url}/api/1/products",
                headers=self.headers,
                timeout=10
            )

            # Try to refresh token if unauthorized
            if response.status_code == 401 and self.refresh_token:
                logger.info("Access token expired, refreshing...")
                self.refresh_access_token()
                response = requests.get(
                    f"{self.base_url}/api/1/products",
                    headers=self.headers,
                    timeout=10
                )

            response.raise_for_status()
            logger.info(f"Fleet API connection successful - Status: {response.status_code}")
            return True, "Connected"
        except requests.exceptions.RequestException as e:
            logger.error(f"Fleet API connection failed: {e}")
            return False, str(e)

    def get_energy_sites(self):
        """Get all energy sites (Powerwalls, Solar)"""
        try:
            logger.info("Fetching Tesla energy sites via Fleet API")
            response = requests.get(
                f"{self.base_url}/api/1/products",
                headers=self.headers,
                timeout=10
            )

            # Auto-refresh on 401
            if response.status_code == 401 and self.refresh_token:
                self.refresh_access_token()
                response = requests.get(
                    f"{self.base_url}/api/1/products",
                    headers=self.headers,
                    timeout=10
                )

            response.raise_for_status()
            data = response.json()

            # Filter for energy sites only
            energy_sites = [p for p in data.get('response', []) if 'energy_site_id' in p]
            logger.info(f"Found {len(energy_sites)} Tesla energy sites via Fleet API")
            return energy_sites
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching energy sites via Fleet API: {e}")
            return []

    def get_site_status(self, site_id):
        """Get status of a specific energy site"""
        try:
            logger.info(f"Fetching site status for {site_id} via Fleet API")
            response = requests.get(
                f"{self.base_url}/api/1/energy_sites/{site_id}/live_status",
                headers=self.headers,
                timeout=10
            )

            if response.status_code == 401 and self.refresh_token:
                self.refresh_access_token()
                response = requests.get(
                    f"{self.base_url}/api/1/energy_sites/{site_id}/live_status",
                    headers=self.headers,
                    timeout=10
                )

            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully fetched site status via Fleet API")
            return data.get('response', {})
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching site status via Fleet API: {e}")
            return None

    def get_site_info(self, site_id):
        """Get detailed information about a site"""
        try:
            logger.info(f"Fetching site info for {site_id} via Fleet API")
            response = requests.get(
                f"{self.base_url}/api/1/energy_sites/{site_id}/site_info",
                headers=self.headers,
                timeout=10
            )

            if response.status_code == 401 and self.refresh_token:
                self.refresh_access_token()
                response = requests.get(
                    f"{self.base_url}/api/1/energy_sites/{site_id}/site_info",
                    headers=self.headers,
                    timeout=10
                )

            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully fetched site info via Fleet API")
            return data.get('response', {})
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching site info via Fleet API: {e}")
            return None

    def set_operation_mode(self, site_id, mode):
        """Set Powerwall operation mode"""
        try:
            valid_modes = ['self_consumption', 'autonomous', 'backup']
            if mode not in valid_modes:
                logger.error(f"Invalid operation mode: {mode}")
                return None

            logger.info(f"Setting operation mode to '{mode}' for site {site_id} via Fleet API")
            response = requests.post(
                f"{self.base_url}/api/1/energy_sites/{site_id}/operation",
                headers=self.headers,
                json={"default_real_mode": mode},
                timeout=30
            )

            if response.status_code == 401 and self.refresh_token:
                self.refresh_access_token()
                response = requests.post(
                    f"{self.base_url}/api/1/energy_sites/{site_id}/operation",
                    headers=self.headers,
                    json={"default_real_mode": mode},
                    timeout=30
                )

            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully set operation mode to '{mode}' via Fleet API")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting operation mode via Fleet API: {e}")
            return None

    def set_backup_reserve(self, site_id, backup_reserve_percent):
        """Set backup reserve percentage"""
        try:
            if not 0 <= backup_reserve_percent <= 100:
                logger.error(f"Invalid backup reserve: {backup_reserve_percent}")
                return None

            logger.info(f"Setting backup reserve to {backup_reserve_percent}% via Fleet API")
            response = requests.post(
                f"{self.base_url}/api/1/energy_sites/{site_id}/backup",
                headers=self.headers,
                json={"backup_reserve_percent": backup_reserve_percent},
                timeout=30
            )

            if response.status_code == 401 and self.refresh_token:
                self.refresh_access_token()
                response = requests.post(
                    f"{self.base_url}/api/1/energy_sites/{site_id}/backup",
                    headers=self.headers,
                    json={"backup_reserve_percent": backup_reserve_percent},
                    timeout=30
                )

            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully set backup reserve to {backup_reserve_percent}% via Fleet API")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting backup reserve via Fleet API: {e}")
            return None

    def set_time_based_control_settings(self, site_id, tou_settings):
        """Set TOU (Time of Use) tariff settings"""
        try:
            logger.info(f"Setting TOU schedule for site {site_id} via Fleet API")
            response = requests.post(
                f"{self.base_url}/api/1/energy_sites/{site_id}/time_of_use_settings",
                headers=self.headers,
                json=tou_settings,
                timeout=30
            )

            if response.status_code == 401 and self.refresh_token:
                self.refresh_access_token()
                response = requests.post(
                    f"{self.base_url}/api/1/energy_sites/{site_id}/time_of_use_settings",
                    headers=self.headers,
                    json=tou_settings,
                    timeout=30
                )

            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully set TOU schedule via Fleet API")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting TOU schedule via Fleet API: {e}")
            return None

    def set_grid_export_rule(self, site_id, export_rule):
        """Set grid export rule (never, pv_only, battery_ok)"""
        try:
            valid_rules = ['never', 'pv_only', 'battery_ok']
            if export_rule not in valid_rules:
                logger.error(f"Invalid export rule: {export_rule}")
                return None

            logger.info(f"Setting grid export rule to '{export_rule}' via Fleet API")
            response = requests.post(
                f"{self.base_url}/api/1/energy_sites/{site_id}/grid_import_export",
                headers=self.headers,
                json={"customer_preferred_export_rule": export_rule},
                timeout=10
            )

            if response.status_code == 401 and self.refresh_token:
                self.refresh_access_token()
                response = requests.post(
                    f"{self.base_url}/api/1/energy_sites/{site_id}/grid_import_export",
                    headers=self.headers,
                    json={"customer_preferred_export_rule": export_rule},
                    timeout=10
                )

            response.raise_for_status()
            data = response.json()

            # Log full response for debugging
            logger.debug(f"Set grid export rule response: {data}")

            # Check if the response indicates actual success
            if isinstance(data, dict) and 'response' in data:
                response_data = data['response']
                if isinstance(response_data, dict) and 'result' in response_data:
                    if not response_data['result']:
                        reason = response_data.get('reason', 'Unknown reason')
                        logger.error(f"❌ Set grid export rule failed: {reason}")
                        logger.error(f"Full response: {data}")
                        return None

            logger.info(f"Successfully set grid export rule to '{export_rule}' via Fleet API")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting grid export rule via Fleet API: {e}")
            return None

    def get_grid_import_export(self, site_id):
        """
        Get current grid import/export settings for the Powerwall

        For VPP/Amber subscribers, customer_preferred_export_rule is not returned.
        Instead, check components.non_export_configured:
        - True = export is NEVER (non-export configured)
        - False/missing = export is allowed (battery_ok equivalent)
        """
        try:
            logger.info(f"Getting grid import/export settings for site {site_id} via Fleet API")
            site_info = self.get_site_info(site_id)

            if site_info:
                # Fields can be at top level OR inside 'components' depending on API/firmware version
                components = site_info.get('components', {})

                # Try components first, then top level as fallback
                export_rule = components.get('customer_preferred_export_rule') or site_info.get('customer_preferred_export_rule')
                disallow_charge = components.get('disallow_charge_from_grid_with_solar_installed') or site_info.get('disallow_charge_from_grid_with_solar_installed')
                non_export_configured = components.get('non_export_configured') or site_info.get('components_non_export_configured')

                # VPP users: derive export rule from non_export_configured
                if export_rule is None and non_export_configured is not None:
                    export_rule = 'never' if non_export_configured else 'battery_ok'
                    logger.info(f"VPP user detected: derived export_rule='{export_rule}' from non_export_configured={non_export_configured}")

                settings = {
                    'customer_preferred_export_rule': export_rule,
                    'disallow_charge_from_grid_with_solar_installed': disallow_charge,
                    'components_non_export_configured': non_export_configured,
                }
                logger.info(f"Current grid export settings: {settings}")
                return settings
            else:
                logger.error(f"Failed to get site_info for {site_id}")
                return None
        except Exception as e:
            logger.error(f"Error getting grid import/export settings via Fleet API: {e}")
            return None

    def set_grid_charging_enabled(self, site_id, enabled: bool):
        """
        Enable or disable grid charging (imports) for the Powerwall.

        Args:
            site_id: Energy site ID
            enabled: True to allow grid charging, False to disallow

        Uses the disallow_charge_from_grid_with_solar_installed field:
            - True = grid charging DISABLED
            - False = grid charging ENABLED (default)

        Returns:
            dict: Response data or None on error
        """
        # Note: The field is inverted - True means charging is DISALLOWED
        disallow_value = not enabled

        try:
            logger.info(f"Setting grid charging {'enabled' if enabled else 'disabled'} for site {site_id} via Fleet API")

            url = f"{self.base_url}/api/1/energy_sites/{site_id}/grid_import_export"
            payload = {
                "disallow_charge_from_grid_with_solar_installed": disallow_value
            }

            logger.debug(f"Fleet API request: POST {url} with payload: {payload}")

            response = requests.post(url, headers=self.headers, json=payload, timeout=30)

            # Handle token refresh on 401
            if response.status_code == 401:
                logger.warning("Fleet API token expired, attempting refresh...")
                if self._refresh_token():
                    response = requests.post(url, headers=self.headers, json=payload, timeout=30)
                else:
                    logger.error("Token refresh failed")
                    return None

            response.raise_for_status()
            data = response.json()
            logger.debug(f"Fleet API response: {data}")

            # Check for actual success in response body
            response_data = data.get('response', data)
            if isinstance(response_data, dict) and 'result' in response_data:
                if not response_data['result']:
                    reason = response_data.get('reason', 'Unknown reason')
                    logger.error(f"Set grid charging failed: {reason}")
                    return None

            logger.info(f"✅ Grid charging {'enabled' if enabled else 'disabled'} successfully for site {site_id}")
            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting grid charging via Fleet API: {e}")
            return None

    def get_calendar_history(self, site_id, kind='energy', period='day', end_date=None, timezone='Australia/Brisbane'):
        """
        Get historical energy data from Tesla calendar history via Fleet API

        Args:
            site_id: Energy site ID
            kind: 'energy' or 'power'
            period: 'day', 'week', 'month', 'year', or 'lifetime'
            end_date: End date (datetime string with timezone, e.g., '2025-10-26T23:59:59+10:00')
            timezone: IANA timezone string (e.g., 'Australia/Brisbane', 'America/New_York')

        Returns:
            dict: Calendar history data with time_series array
        """
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            # Default to current time in user's timezone if no end_date provided
            if not end_date:
                user_tz = ZoneInfo(timezone)
                now = datetime.now(user_tz)
                # Use 23:59:59 to avoid midnight issues
                end_dt = now.replace(hour=23, minute=59, second=59)
                end_date = end_dt.isoformat()

            logger.info(f"Fetching calendar history for site {site_id} via Fleet API: kind={kind}, period={period}, end_date={end_date}, timezone={timezone}")

            params = {
                'kind': kind,
                'period': period,
                'end_date': end_date,
                'time_zone': timezone
            }

            response = requests.get(
                f"{self.base_url}/api/1/energy_sites/{site_id}/calendar_history",
                headers=self.headers,
                params=params,
                timeout=15
            )

            # Auto-refresh on 401
            if response.status_code == 401 and self.refresh_token:
                self.refresh_access_token()
                response = requests.get(
                    f"{self.base_url}/api/1/energy_sites/{site_id}/calendar_history",
                    headers=self.headers,
                    params=params,
                    timeout=15
                )

            response.raise_for_status()
            data = response.json()
            result = data.get('response', {})
            time_series = result.get('time_series', [])
            logger.info(f"Successfully fetched calendar history via Fleet API: {len(time_series)} records returned for period='{period}'")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching calendar history via Fleet API: {e}")
            return None

    def get_current_tariff(self, site_id):
        """
        Get the current TOU tariff from Tesla Powerwall via Fleet API

        Returns the complete tariff structure that's currently programmed into the Powerwall.
        This can be saved and later restored.

        Returns:
            dict: Complete tariff structure or None if error
        """
        try:
            logger.info(f"Fetching current tariff for site {site_id} via Fleet API")

            # Get site_info which includes TOU settings
            site_info = self.get_site_info(site_id)
            if not site_info:
                logger.error("Failed to fetch site info")
                return None

            # Extract the tariff from site_info
            tariff = site_info.get('tariff_content_v2')

            if tariff:
                logger.info(f"Successfully extracted current tariff: {tariff.get('name', 'Unknown')}")
                logger.debug(f"Tariff keys: {list(tariff.keys())}")
                return tariff
            else:
                logger.warning("No tariff found in site_info")
                logger.debug(f"Site info keys: {list(site_info.keys())}")
                return None

        except Exception as e:
            logger.error(f"Error getting current tariff via Fleet API: {e}")
            return None

    def get_battery_level(self, site_id):
        """Get current battery level via Fleet API"""
        try:
            status = self.get_site_status(site_id)
            if status:
                battery_level = status.get('percentage_charged', 0)
                logger.info(f"Battery level: {battery_level}%")
                return battery_level
            return None
        except Exception as e:
            logger.error(f"Error getting battery level via Fleet API: {e}")
            return None

    def get_operation_mode(self, site_id):
        """
        Get the current Powerwall operation mode via Fleet API

        Args:
            site_id: Energy site ID

        Returns:
            str: Current operation mode ('self_consumption', 'backup', 'autonomous') or None if error
        """
        try:
            logger.info(f"Getting operation mode for site {site_id} via Fleet API")
            site_info = self.get_site_info(site_id)

            if site_info:
                mode = site_info.get('default_real_mode')
                if mode:
                    logger.info(f"Current operation mode: {mode}")
                    return mode
                else:
                    logger.warning(f"default_real_mode not found in site_info")
                    return None
            else:
                logger.error(f"Failed to get site_info for {site_id}")
                return None
        except Exception as e:
            logger.error(f"Error getting operation mode via Fleet API: {e}")
            return None

    def set_tariff_rate(self, site_id, tariff_content):
        """
        Set the electricity tariff/rate plan for the site via Fleet API

        Uses the time_of_use_settings endpoint with tariff_content_v2

        Args:
            site_id: Energy site ID
            tariff_content: Dictionary with complete tariff structure (v2 format)
        """
        try:
            logger.info(f"Setting tariff rate for site {site_id} via Fleet API")
            logger.debug(f"Tariff structure keys: {list(tariff_content.keys())}")

            # The payload structure for time_of_use_settings with tariff
            payload = {
                "tou_settings": {
                    "tariff_content_v2": tariff_content
                }
            }

            # Log a sample of the tariff being sent for debugging
            if 'energy_charges' in tariff_content and tariff_content['energy_charges']:
                energy_charges_keys = list(tariff_content['energy_charges'].keys())
                logger.debug(f"Tariff energy_charges seasons: {energy_charges_keys}")

            # Debug: Check if tou_periods are being sent
            if 'seasons' in tariff_content and 'Summer' in tariff_content['seasons']:
                if 'tou_periods' in tariff_content['seasons']['Summer']:
                    sample_period = list(tariff_content['seasons']['Summer']['tou_periods'].items())[0]
                    logger.info(f"DEBUG: Sending tou_periods - First period: {sample_period[0]} = {sample_period[1]}")
                else:
                    logger.warning(f"DEBUG: No tou_periods in tariff being sent!")

            response = requests.post(
                f"{self.base_url}/api/1/energy_sites/{site_id}/time_of_use_settings",
                headers=self.headers,
                json=payload,
                timeout=30
            )

            # Auto-refresh on 401
            if response.status_code == 401 and self.refresh_token:
                self.refresh_access_token()
                response = requests.post(
                    f"{self.base_url}/api/1/energy_sites/{site_id}/time_of_use_settings",
                    headers=self.headers,
                    json=payload,
                    timeout=30
                )

            logger.info(f"Set tariff via Fleet API response status: {response.status_code}")

            response.raise_for_status()
            data = response.json()

            logger.info(f"Fleet API response: {data}")

            # Check if the response indicates success
            if isinstance(data, dict):
                if 'response' in data:
                    response_data = data['response']
                    if isinstance(response_data, dict) and 'result' in response_data:
                        if not response_data['result']:
                            reason = response_data.get('reason', 'Unknown reason')
                            logger.error(f"Tariff update failed: {reason}")
                            logger.error(f"Full response: {data}")
                            return None

            logger.info(f"Successfully set tariff rate for site {site_id} via Fleet API")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting tariff rate via Fleet API: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Error response: {e.response.text}")
            return None


class TeslemetryAPIClient(TeslaAPIClientBase):
    """Client for Teslemetry API (Tesla API proxy service)"""

    BASE_URL = "https://api.teslemetry.com"

    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = self.BASE_URL
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": TESLA_SYNC_USER_AGENT,
        }
        logger.info("TeslemetryAPIClient initialized")

    def test_connection(self):
        """Test the API connection"""
        try:
            logger.info("Testing Teslemetry API connection")
            response = requests.get(
                f"{self.base_url}/api/1/products",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            logger.info(f"Teslemetry API connection successful - Status: {response.status_code}")
            return True, "Connected"
        except requests.exceptions.RequestException as e:
            logger.error(f"Teslemetry API connection failed: {e}")
            return False, str(e)

    def get_energy_sites(self):
        """Get all energy sites (Powerwalls, Solar)"""
        try:
            logger.info("Fetching Tesla energy sites via Teslemetry")
            response = requests.get(
                f"{self.base_url}/api/1/products",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            # Filter for energy sites only
            energy_sites = [p for p in data.get('response', []) if 'energy_site_id' in p]
            logger.info(f"Found {len(energy_sites)} Tesla energy sites via Teslemetry")
            return energy_sites
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching energy sites via Teslemetry: {e}")
            return []

    def get_site_status(self, site_id):
        """Get status of a specific energy site"""
        try:
            # First, get the list of products to find the energy site
            logger.info(f"Getting products list to find energy site {site_id}")
            products_response = requests.get(
                f"{self.base_url}/api/1/products",
                headers=self.headers,
                timeout=10
            )
            products_response.raise_for_status()
            products_data = products_response.json()
            logger.info(f"Products response: {products_data}")

            # Find the energy site in products
            energy_site = None
            for product in products_data.get('response', []):
                if product.get('energy_site_id') and str(product.get('energy_site_id')) == str(site_id):
                    energy_site = product
                    break
                # Also try resource_id field
                if product.get('resource') and str(product.get('resource')) == str(site_id):
                    energy_site = product
                    break

            if not energy_site:
                logger.error(f"Energy site {site_id} not found in products list")
                logger.error(f"Available products: {products_data}")
                return None

            logger.info(f"Found energy site in products: {energy_site}")

            # Try to get live_status using the correct endpoint
            site_id_numeric = energy_site.get('energy_site_id') or site_id
            logger.info(f"Fetching site status for {site_id_numeric} via Teslemetry")

            # Teslemetry uses /api/1/energy_sites/{id}/live_status
            response = requests.get(
                f"{self.base_url}/api/1/energy_sites/{site_id_numeric}/live_status",
                headers=self.headers,
                timeout=10
            )

            # Log response before raising
            logger.info(f"Teslemetry live_status response status: {response.status_code}")
            if response.status_code != 200:
                logger.error(f"Teslemetry error response: {response.text}")

            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully fetched site status via Teslemetry")
            logger.info(f"Teslemetry response keys: {list(data.keys())}")
            logger.info(f"Full Teslemetry site status response: {data}")
            return data.get('response', {})
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching site status via Teslemetry: {e}")
            return None

    def get_site_info(self, site_id):
        """Get detailed information about a site"""
        try:
            logger.info(f"Fetching site info for {site_id} via Teslemetry")
            response = requests.get(
                f"{self.base_url}/api/1/energy_sites/{site_id}/site_info",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully fetched site info via Teslemetry")
            return data.get('response', {})
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching site info via Teslemetry: {e}")
            return None

    def get_current_tariff(self, site_id):
        """
        Get the current TOU tariff from Tesla Powerwall

        Returns the complete tariff structure that's currently programmed into the Powerwall.
        This can be saved and later restored.

        Returns:
            dict: Complete tariff structure or None if error
        """
        try:
            logger.info(f"Fetching current tariff for site {site_id} via Teslemetry")

            # Get site_info which includes TOU settings
            site_info = self.get_site_info(site_id)
            if not site_info:
                logger.error("Failed to fetch site info")
                return None

            # Extract the tariff from site_info
            # Teslemetry returns it as 'tariff_content_v2' (not 'utility_tariff_content_v2')
            tariff = site_info.get('tariff_content_v2')

            if tariff:
                logger.info(f"Successfully extracted current tariff: {tariff.get('name', 'Unknown')}")
                logger.debug(f"Tariff keys: {list(tariff.keys())}")
                return tariff
            else:
                logger.warning("No tariff found in site_info")
                logger.debug(f"Site info keys: {list(site_info.keys())}")
                return None

        except Exception as e:
            logger.error(f"Error getting current tariff: {e}")
            return None

    def get_battery_level(self, site_id):
        """Get current battery level"""
        try:
            status = self.get_site_status(site_id)
            if status:
                battery_level = status.get('percentage_charged', 0)
                logger.info(f"Battery level: {battery_level}%")
                return battery_level
            return None
        except Exception as e:
            logger.error(f"Error getting battery level via Teslemetry: {e}")
            return None

    def get_calendar_history(self, site_id, kind='energy', period='day', end_date=None, timezone='Australia/Brisbane'):
        """
        Get historical energy data from Tesla calendar history

        Args:
            site_id: Energy site ID
            kind: 'energy' or 'power'
            period: 'day', 'week', 'month', 'year', or 'lifetime'
            end_date: End date (datetime string with timezone, e.g., '2025-10-26T23:59:59+10:00')
            timezone: IANA timezone string (e.g., 'Australia/Brisbane', 'America/New_York')

        Returns:
            dict: Calendar history data with time_series array
        """
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            # Default to current time in user's timezone if no end_date provided
            # Use 11:59 PM to avoid midnight issues
            if not end_date:
                user_tz = ZoneInfo(timezone)
                now = datetime.now(user_tz)
                # Use 23:59:59 to avoid midnight issues
                end_dt = now.replace(hour=23, minute=59, second=59)
                end_date = end_dt.isoformat()

            logger.info(f"Fetching calendar history for site {site_id} via Teslemetry: kind={kind}, period={period}, end_date={end_date}, timezone={timezone}")

            params = {
                'kind': kind,
                'period': period,
                'end_date': end_date,
                'time_zone': timezone
            }

            response = requests.get(
                f"{self.base_url}/api/1/energy_sites/{site_id}/calendar_history",
                headers=self.headers,
                params=params,
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            result = data.get('response', {})
            time_series = result.get('time_series', [])
            logger.info(f"Successfully fetched calendar history via Teslemetry: {len(time_series)} records returned for period='{period}'")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching calendar history via Teslemetry: {e}")
            return None

    def get_operation_mode(self, site_id):
        """
        Get the current Powerwall operation mode

        Args:
            site_id: Energy site ID

        Returns:
            str: Current operation mode ('self_consumption', 'backup', 'autonomous') or None if error
        """
        try:
            logger.info(f"Getting operation mode for site {site_id}")
            site_info = self.get_site_info(site_id)

            if site_info:
                mode = site_info.get('default_real_mode')
                if mode:
                    logger.info(f"Current operation mode: {mode}")
                    return mode
                else:
                    logger.warning(f"default_real_mode not found in site_info")
                    return None
            else:
                logger.error(f"Failed to get site_info for {site_id}")
                return None
        except Exception as e:
            logger.error(f"Error getting operation mode: {e}")
            return None

    def set_operation_mode(self, site_id, mode):
        """
        Set the Powerwall operation mode

        Args:
            site_id: Energy site ID
            mode: Operation mode - 'self_consumption', 'backup', 'autonomous'
        """
        try:
            logger.info(f"Setting operation mode to {mode} for site {site_id}")
            response = requests.post(
                f"{self.base_url}/api/1/energy_sites/{site_id}/operation",
                headers=self.headers,
                json={"default_real_mode": mode},
                timeout=10
            )

            logger.info(f"Set operation mode response status: {response.status_code}")
            if response.status_code not in [200, 201, 202]:
                logger.error(f"Error response: {response.text}")

            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully set operation mode to {mode}")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting operation mode via Teslemetry: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Error response: {e.response.text}")
            return None

    def set_backup_reserve(self, site_id, backup_reserve_percent):
        """
        Set the backup reserve percentage

        Args:
            site_id: Energy site ID
            backup_reserve_percent: Backup reserve percentage (0-100)
        """
        try:
            logger.info(f"Setting backup reserve to {backup_reserve_percent}% for site {site_id}")
            response = requests.post(
                f"{self.base_url}/api/1/energy_sites/{site_id}/backup",
                headers=self.headers,
                json={"backup_reserve_percent": backup_reserve_percent},
                timeout=10
            )

            logger.info(f"Set backup reserve response status: {response.status_code}")
            if response.status_code not in [200, 201, 202]:
                logger.error(f"Error response: {response.text}")

            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully set backup reserve to {backup_reserve_percent}%")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting backup reserve via Teslemetry: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Error response: {e.response.text}")
            return None

    def get_time_based_control_settings(self, site_id):
        """Get current time-based control settings"""
        try:
            url = f"{self.base_url}/api/1/energy_sites/{site_id}/time_of_use_settings"
            logger.info(f"Getting time-based control settings for site {site_id}")
            logger.info(f"Teslemetry API URL: {url}")
            logger.debug(f"Request headers: {dict((k,v if k != 'Authorization' else '***') for k,v in self.headers.items())}")

            response = requests.get(
                url,
                headers=self.headers,
                timeout=10
            )
            logger.info(f"Response status code: {response.status_code}")
            logger.debug(f"Response headers: {dict(response.headers)}")
            logger.debug(f"Raw response text: {response.text}")

            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully fetched time-based control settings")
            logger.debug(f"Parsed JSON response: {data}")

            # Extract response field
            result = data.get('response', {})
            logger.info(f"Returning {len(result) if result else 0} items from response field")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"Error getting time-based control settings: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Error response status: {e.response.status_code}")
                logger.error(f"Error response text: {e.response.text}")
            return None

    def set_time_based_control_settings(self, site_id, tou_settings):
        """
        Set time-based control settings with schedule

        Args:
            site_id: Energy site ID
            tou_settings: Dictionary with TOU schedule settings
        """
        try:
            logger.info(f"Setting time-based control settings for site {site_id}")
            logger.info(f"TOU settings: {tou_settings}")

            response = requests.post(
                f"{self.base_url}/api/1/energy_sites/{site_id}/time_of_use_settings",
                headers=self.headers,
                json=tou_settings,
                timeout=10
            )

            logger.info(f"Set TOU settings response status: {response.status_code}")
            if response.status_code not in [200, 201, 202]:
                logger.error(f"Error response: {response.text}")

            response.raise_for_status()
            data = response.json()
            logger.info(f"Successfully set time-based control settings")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting time-based control settings: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Error response: {e.response.text}")
            return None

    def set_tariff_rate(self, site_id, tariff_content):
        """
        Set the electricity tariff/rate plan for the site

        Uses the time_of_use_settings endpoint with tariff_content_v2

        Args:
            site_id: Energy site ID
            tariff_content: Dictionary with complete tariff structure (v2 format)
        """
        try:
            logger.info(f"Setting tariff rate for site {site_id}")
            logger.debug(f"Tariff structure keys: {list(tariff_content.keys())}")

            # The payload structure for time_of_use_settings with tariff
            payload = {
                "tou_settings": {
                    "tariff_content_v2": tariff_content
                }
            }

            # Log a sample of the tariff being sent for debugging
            if 'energy_charges' in tariff_content and tariff_content['energy_charges']:
                energy_charges_keys = list(tariff_content['energy_charges'].keys())
                logger.debug(f"Tariff energy_charges seasons: {energy_charges_keys}")

            # Debug: Check if tou_periods are being sent
            if 'seasons' in tariff_content and 'Summer' in tariff_content['seasons']:
                if 'tou_periods' in tariff_content['seasons']['Summer']:
                    sample_period = list(tariff_content['seasons']['Summer']['tou_periods'].items())[0]
                    logger.info(f"DEBUG: Sending tou_periods - First period: {sample_period[0]} = {sample_period[1]}")
                else:
                    logger.warning(f"DEBUG: No tou_periods in tariff being sent!")

            response = requests.post(
                f"{self.base_url}/api/1/energy_sites/{site_id}/time_of_use_settings",
                headers=self.headers,
                json=payload,
                timeout=30  # Longer timeout for tariff updates
            )

            logger.info(f"Set tariff via TOU settings response status: {response.status_code}")

            response.raise_for_status()
            data = response.json()

            # Log the full response to debug tariff update issues
            logger.info(f"Teslemetry API response: {data}")

            # Check if the response indicates success
            if isinstance(data, dict):
                if 'response' in data:
                    response_data = data['response']
                    if isinstance(response_data, dict) and 'result' in response_data:
                        if not response_data['result']:
                            reason = response_data.get('reason', 'Unknown reason')
                            logger.error(f"Tariff update failed: {reason}")
                            logger.error(f"Full response: {data}")
                            return None

            logger.info(f"Successfully set tariff rate for site {site_id}")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting tariff rate: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Error response: {e.response.text}")
            return None

    def get_grid_import_export(self, site_id):
        """
        Get current grid import/export settings for the Powerwall

        For VPP/Amber subscribers, customer_preferred_export_rule is not returned.
        Instead, check components_non_export_configured:
        - True = export is NEVER (non-export configured)
        - False/missing = export is allowed (battery_ok equivalent)

        Args:
            site_id: Energy site ID

        Returns:
            dict: Grid import/export settings including customer_preferred_export_rule, or None on error
            Example: {
                'customer_preferred_export_rule': 'pv_only',  # 'never', 'pv_only', or 'battery_ok'
                'disallow_charge_from_grid_with_solar_installed': True,
                'components_non_export_configured': False  # VPP indicator
            }
        """
        try:
            logger.info(f"Getting grid import/export settings for site {site_id}")
            site_info = self.get_site_info(site_id)

            if site_info:
                # Fields can be at top level OR inside 'components' depending on API/firmware version
                components = site_info.get('components', {})

                # Try components first, then top level as fallback
                export_rule = components.get('customer_preferred_export_rule') or site_info.get('customer_preferred_export_rule')
                disallow_charge = components.get('disallow_charge_from_grid_with_solar_installed') or site_info.get('disallow_charge_from_grid_with_solar_installed')
                non_export_configured = components.get('non_export_configured') or site_info.get('components_non_export_configured')

                # If customer_preferred_export_rule is missing but non_export_configured exists,
                # this is a VPP user - derive the export rule from non_export_configured
                if export_rule is None and non_export_configured is not None:
                    export_rule = 'never' if non_export_configured else 'battery_ok'
                    logger.info(f"VPP user detected: derived export_rule='{export_rule}' from non_export_configured={non_export_configured}")

                settings = {
                    'customer_preferred_export_rule': export_rule,
                    'disallow_charge_from_grid_with_solar_installed': disallow_charge,
                    'components_non_export_configured': non_export_configured,  # Include for debugging
                }
                logger.info(f"Current grid export settings: {settings}")
                return settings
            else:
                logger.error(f"Failed to get site_info for {site_id}")
                return None
        except Exception as e:
            logger.error(f"Error getting grid import/export settings: {e}")
            return None

    def set_grid_export_rule(self, site_id, export_rule):
        """
        Set the grid export rule for the Powerwall

        Args:
            site_id: Energy site ID
            export_rule: Export mode - 'never', 'pv_only', or 'battery_ok'
                - 'never': No export to grid (Permanent Non Export)
                - 'pv_only': Only solar can export (Solar Only Export)
                - 'battery_ok': Both battery and solar can export

        Returns:
            dict: Response data or None on error
        """
        valid_rules = ['never', 'pv_only', 'battery_ok']
        if export_rule not in valid_rules:
            logger.error(f"Invalid export rule: {export_rule}. Must be one of {valid_rules}")
            return None

        try:
            logger.info(f"Setting grid export rule to '{export_rule}' for site {site_id}")
            response = requests.post(
                f"{self.base_url}/api/1/energy_sites/{site_id}/grid_import_export",
                headers=self.headers,
                json={"customer_preferred_export_rule": export_rule},
                timeout=10
            )

            logger.info(f"Set grid export rule response status: {response.status_code}")
            if response.status_code not in [200, 201, 202]:
                logger.error(f"Error response: {response.text}")

            response.raise_for_status()
            data = response.json()

            # Log full response for debugging
            logger.debug(f"Set grid export rule response: {data}")

            # Check if the response indicates actual success (like set_tariff_rate does)
            if isinstance(data, dict) and 'response' in data:
                response_data = data['response']
                if isinstance(response_data, dict) and 'result' in response_data:
                    if not response_data['result']:
                        reason = response_data.get('reason', 'Unknown reason')
                        logger.error(f"❌ Set grid export rule failed: {reason}")
                        logger.error(f"Full response: {data}")
                        return None

            logger.info(f"Successfully set grid export rule to '{export_rule}'")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting grid export rule via Teslemetry: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Error response: {e.response.text}")
            return None

    def set_grid_charging_enabled(self, site_id, enabled: bool):
        """
        Enable or disable grid charging (imports) for the Powerwall.

        Args:
            site_id: Energy site ID
            enabled: True to allow grid charging, False to disallow

        Uses the disallow_charge_from_grid_with_solar_installed field:
            - True = grid charging DISABLED
            - False = grid charging ENABLED (default)

        Returns:
            dict: Response data or None on error
        """
        # Note: The field is inverted - True means charging is DISALLOWED
        disallow_value = not enabled

        try:
            logger.info(f"Setting grid charging {'enabled' if enabled else 'disabled'} for site {site_id} via Teslemetry")

            url = f"{self.base_url}/api/1/energy_sites/{site_id}/grid_import_export"
            payload = {
                "disallow_charge_from_grid_with_solar_installed": disallow_value
            }

            logger.debug(f"Teslemetry request: POST {url} with payload: {payload}")

            response = requests.post(url, headers=self.headers, json=payload, timeout=30)

            logger.info(f"Set grid charging response status: {response.status_code}")
            if response.status_code not in [200, 201, 202]:
                logger.error(f"Error response: {response.text}")

            response.raise_for_status()
            data = response.json()

            # Log full response for debugging
            logger.debug(f"Set grid charging response: {data}")

            # Check if the response indicates actual success
            if isinstance(data, dict) and 'response' in data:
                response_data = data['response']
                if isinstance(response_data, dict) and 'result' in response_data:
                    if not response_data['result']:
                        reason = response_data.get('reason', 'Unknown reason')
                        logger.error(f"❌ Set grid charging failed: {reason}")
                        logger.error(f"Full response: {data}")
                        return None

            logger.info(f"✅ Grid charging {'enabled' if enabled else 'disabled'} successfully for site {site_id}")
            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting grid charging via Teslemetry: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Error response: {e.response.text}")
            return None


def get_amber_client(user):
    """Get an Amber API client for the user with their selected site ID"""
    if not user.amber_api_token_encrypted:
        logger.warning(f"No Amber token for user {user.email}")
        return None

    try:
        api_token = decrypt_token(user.amber_api_token_encrypted)
        # Pass user's selected Amber site ID to the client
        site_id = getattr(user, 'amber_site_id', None)
        return AmberAPIClient(api_token, site_id=site_id)
    except Exception as e:
        logger.error(f"Error creating Amber client: {e}")
        return None


class AEMOAPIClient:
    """Client for AEMO (Australian Energy Market Operator) NEM Data API

    Fetches real-time electricity pricing data from the National Electricity Market (NEM).
    No authentication required - uses public API endpoints.
    """

    BASE_URL = "https://visualisations.aemo.com.au/aemo/apps/api/report/ELEC_NEM_SUMMARY"

    # NEM Regions
    REGIONS = {
        'NSW1': 'New South Wales',
        'QLD1': 'Queensland',
        'VIC1': 'Victoria',
        'SA1': 'South Australia',
        'TAS1': 'Tasmania'
    }

    # Class-level cache for pre-dispatch forecast (shared across instances)
    # AEMO updates pre-dispatch files every 30 minutes, so we cache to avoid redundant downloads
    _predispatch_cache = {
        'filename': None,      # Last downloaded filename
        'data': {},            # Parsed data by region: {'NSW1': [...], 'QLD1': [...], ...}
        'timestamp': None      # When the cache was populated
    }

    def __init__(self):
        """Initialize AEMO API client (no auth required)"""
        logger.info("AEMOAPIClient initialized")

    def get_current_prices(self):
        """Get current 5-minute dispatch prices for all NEM regions

        Returns:
            dict: Price data for all regions or None on error
            Example: {
                'NSW1': {'price': 72.06, 'timestamp': '2025-11-08T21:00:00', 'status': 'FIRM'},
                'QLD1': {'price': 69.89, 'timestamp': '2025-11-08T21:00:00', 'status': 'FIRM'},
                ...
            }
        """
        try:
            logger.info("Fetching current AEMO NEM prices")
            response = requests.get(self.BASE_URL, timeout=15)
            response.raise_for_status()
            data = response.json()

            # Extract regional prices from the ELEC_NEM_SUMMARY data
            prices = {}

            if 'ELEC_NEM_SUMMARY' in data:
                for item in data['ELEC_NEM_SUMMARY']:
                    region = item.get('REGIONID')
                    if region in self.REGIONS:
                        prices[region] = {
                            'price': float(item.get('PRICE', 0)),  # Wholesale price in $/MWh
                            'timestamp': item.get('SETTLEMENTDATE'),
                            'status': item.get('PRICE_STATUS', 'UNKNOWN'),
                            'demand': float(item.get('TOTALDEMAND', 0)),
                            'region_name': self.REGIONS[region]
                        }

            logger.info(f"Successfully fetched AEMO prices for {len(prices)} regions")
            logger.debug(f"AEMO price data: {prices}")
            return prices

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching AEMO prices: {e}")
            return None
        except (KeyError, ValueError) as e:
            logger.error(f"Error parsing AEMO data: {e}")
            return None

    def get_region_price(self, region):
        """Get current price for a specific region

        Args:
            region: Region code (NSW1, QLD1, VIC1, SA1, TAS1)

        Returns:
            dict: Price data for the region or None
        """
        if region not in self.REGIONS:
            logger.error(f"Invalid region: {region}. Must be one of {list(self.REGIONS.keys())}")
            return None

        prices = self.get_current_prices()
        if prices:
            return prices.get(region)
        return None

    def check_price_spike(self, region, threshold_dollars_per_mwh):
        """Check if current price exceeds threshold (price spike detection)

        Args:
            region: Region code (NSW1, QLD1, VIC1, SA1, TAS1)
            threshold_dollars_per_mwh: Spike threshold in $/MWh (e.g., 300)

        Returns:
            tuple: (is_spike: bool, current_price: float, price_data: dict)
        """
        price_data = self.get_region_price(region)
        if not price_data:
            return False, None, None

        current_price = price_data['price']
        is_spike = current_price >= threshold_dollars_per_mwh

        if is_spike:
            logger.warning(f"PRICE SPIKE DETECTED in {region}: ${current_price}/MWh (threshold: ${threshold_dollars_per_mwh}/MWh)")
        else:
            logger.info(f"Normal price in {region}: ${current_price}/MWh (threshold: ${threshold_dollars_per_mwh}/MWh)")

        return is_spike, current_price, price_data

    def get_price_forecast(self, region, periods=48):
        """Get AEMO 30-min pre-dispatch price forecast.

        Fetches directly from AEMO's NEMWeb pre-dispatch reports (ZIP/CSV).
        Returns data in Amber-compatible format for tariff converter reuse.

        Uses class-level caching to avoid re-downloading the same file.
        AEMO updates pre-dispatch files every 30 minutes.

        Args:
            region: NEM region code (NSW1, QLD1, VIC1, SA1, TAS1)
            periods: Number of 30-minute periods to fetch (default 48 = 24 hours)

        Returns:
            list: Price intervals in Amber-compatible format:
            [
                {
                    'nemTime': '2025-12-13T19:30:00+10:00',
                    'perKwh': 11.0,  # cents/kWh
                    'channelType': 'general',
                    'type': 'ForecastInterval',
                    'duration': 30
                },
                ...
            ]
        """
        import zipfile
        import csv
        import io
        import re
        from datetime import datetime
        import pytz

        if region not in self.REGIONS:
            logger.error(f"Invalid region: {region}. Must be one of {list(self.REGIONS.keys())}")
            return None

        try:
            # Step 1: Get list of available pre-dispatch files from NEMWeb
            index_url = "https://nemweb.com.au/Reports/Current/Predispatch_Reports/"

            response = requests.get(index_url, timeout=30)
            response.raise_for_status()

            # Step 2: Find latest PUBLIC_PREDISPATCH file
            files = re.findall(r'PUBLIC_PREDISPATCH_\d+_\d+_LEGACY\.zip', response.text)
            if not files:
                logger.error("No pre-dispatch files found in AEMO NEMWeb directory")
                return None

            latest_file = sorted(files)[-1]  # Get most recent by timestamp

            # Step 3: Check cache - return cached data if file hasn't changed
            cache = AEMOAPIClient._predispatch_cache
            if cache['filename'] == latest_file and region in cache['data']:
                cached_intervals = cache['data'][region]
                logger.info(f"📦 Using cached AEMO forecast for {region} ({len(cached_intervals) // 2} periods, file: {latest_file})")
                # Return requested number of periods (or all if fewer available)
                return cached_intervals[:periods * 2] if len(cached_intervals) > periods * 2 else cached_intervals

            # Step 4: Download the ZIP file (cache miss or new file)
            file_url = f"{index_url}{latest_file}"
            logger.info(f"⬇️  Downloading AEMO pre-dispatch: {latest_file}")
            zip_response = requests.get(file_url, timeout=60)
            zip_response.raise_for_status()

            # Step 5: Parse CSV from ZIP - extract ALL regions for caching
            aest = pytz.timezone('Australia/Brisbane')  # NEM time (AEST, no DST)
            region_data = {r: [] for r in self.REGIONS}  # Initialize all regions
            seen_timestamps = {r: set() for r in self.REGIONS}  # Track duplicates per region

            with zipfile.ZipFile(io.BytesIO(zip_response.content)) as zf:
                # The ZIP contains a single CSV file with all data tables
                csv_files = [f for f in zf.namelist() if f.endswith('.CSV') or f.endswith('.csv')]
                if not csv_files:
                    logger.error(f"No CSV file in pre-dispatch ZIP: {zf.namelist()}")
                    return None

                logger.debug(f"Found CSV file: {csv_files[0]}")

                with zf.open(csv_files[0]) as f:
                    reader = csv.reader(io.TextIOWrapper(f, encoding='utf-8'))

                    for row in reader:
                        # AEMO pre-dispatch CSV format (PDREGION table):
                        # D,PDREGION,,5,DateTime,RunNo,REGIONID,PeriodDateTime,RRP,...
                        # Column 0: Record type (D = data)
                        # Column 1: Table name (PDREGION)
                        # Column 6: Region ID (NSW1, QLD1, VIC1, SA1, TAS1)
                        # Column 7: Period DateTime (forecast period)
                        # Column 8: RRP in $/MWh
                        if len(row) < 9 or row[0] != 'D':
                            continue

                        try:
                            # Check if this is a PDREGION row (contains price data)
                            table_name = row[1] if len(row) > 1 else ''
                            if table_name != 'PDREGION':
                                continue

                            # Extract region
                            row_region = row[6] if len(row) > 6 else None
                            if row_region not in self.REGIONS:
                                continue

                            # Extract period datetime and RRP
                            datetime_str = row[7] if len(row) > 7 else None
                            rrp_str = row[8] if len(row) > 8 else None

                            if not datetime_str or not rrp_str:
                                continue

                            # Skip duplicates (same timestamp for same region)
                            if datetime_str in seen_timestamps[row_region]:
                                continue
                            seen_timestamps[row_region].add(datetime_str)

                            # Parse datetime (format: YYYY/MM/DD HH:MM:SS)
                            dt = datetime.strptime(datetime_str, '%Y/%m/%d %H:%M:%S')
                            dt = aest.localize(dt)

                            # Parse RRP ($/MWh) and convert to c/kWh
                            rrp = float(rrp_str)
                            price_cents = rrp / 10.0  # $/MWh ÷ 10 = c/kWh

                            # Add import (general) price
                            region_data[row_region].append({
                                'nemTime': dt.isoformat(),
                                'perKwh': price_cents,
                                'channelType': 'general',
                                'type': 'ForecastInterval',
                                'duration': 30
                            })

                            # Add export (feedIn) price - same as import for AEMO
                            # (will be overridden by Flow Power Happy Hour rates)
                            region_data[row_region].append({
                                'nemTime': dt.isoformat(),
                                'perKwh': -price_cents,  # Amber convention: negative = you get paid
                                'channelType': 'feedIn',
                                'type': 'ForecastInterval',
                                'duration': 30
                            })

                        except (ValueError, IndexError) as e:
                            logger.debug(f"Skipping row due to parse error: {e}")
                            continue

            # Sort each region's data by timestamp
            for r in region_data:
                region_data[r].sort(key=lambda x: x['nemTime'])

            # Update cache with all regions
            AEMOAPIClient._predispatch_cache = {
                'filename': latest_file,
                'data': region_data,
                'timestamp': datetime.now(pytz.UTC).isoformat()
            }

            # Log cache update
            region_counts = {r: len(d) // 2 for r, d in region_data.items() if d}
            logger.info(f"✅ Cached AEMO forecast for all regions: {region_counts}")

            # Return requested region's data
            intervals = region_data.get(region, [])
            if not intervals:
                logger.error(f"No price data found for region {region} in pre-dispatch file")
                return None

            logger.info(f"Successfully parsed {len(intervals) // 2} AEMO forecast periods for {region}")
            return intervals[:periods * 2] if len(intervals) > periods * 2 else intervals

        except requests.RequestException as e:
            logger.error(f"Network error fetching AEMO pre-dispatch: {e}")
            return None
        except zipfile.BadZipFile as e:
            logger.error(f"Invalid ZIP file from AEMO: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching AEMO price forecast: {e}")
            import traceback
            logger.debug(f"Traceback: {traceback.format_exc()}")
            return None


def get_tesla_client(user):
    """
    Get a Tesla API client for the user

    Returns either FleetAPIClient or TeslemetryAPIClient based on user configuration.
    Priority: Fleet API > Teslemetry (if both are configured)

    Args:
        user: User model instance

    Returns:
        TeslaAPIClientBase instance (FleetAPIClient or TeslemetryAPIClient) or None
    """

    # Check for Fleet API configuration first
    if user.tesla_api_provider == 'fleet_api' and user.fleet_api_access_token_encrypted:
        try:
            logger.info(f"Using FleetAPIClient (direct) for user {user.email}")
            access_token = decrypt_token(user.fleet_api_access_token_encrypted)
            refresh_token = decrypt_token(user.fleet_api_refresh_token_encrypted) if user.fleet_api_refresh_token_encrypted else None

            # Get client ID and secret - prefer user's saved credentials, fall back to environment
            client_id = None
            client_secret = None
            if user.fleet_api_client_id_encrypted:
                client_id = decrypt_token(user.fleet_api_client_id_encrypted)
            if user.fleet_api_client_secret_encrypted:
                client_secret = decrypt_token(user.fleet_api_client_secret_encrypted)

            # Fall back to environment variables
            if not client_id:
                client_id = os.getenv('TESLA_CLIENT_ID')
            if not client_secret:
                client_secret = os.getenv('TESLA_CLIENT_SECRET')

            return FleetAPIClient(
                access_token=access_token,
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret
            )
        except Exception as e:
            logger.error(f"Error creating Fleet API client: {e}")
            # Fall back to Teslemetry if Fleet API fails
            logger.info("Falling back to Teslemetry")

    # Fall back to Teslemetry (default)
    if user.teslemetry_api_key_encrypted:
        try:
            logger.info(f"Using TeslemetryAPIClient (proxy) for user {user.email}")
            api_key = decrypt_token(user.teslemetry_api_key_encrypted)
            return TeslemetryAPIClient(api_key)
        except Exception as e:
            logger.error(f"Error creating Teslemetry client: {e}")
            return None

    logger.warning(f"No Tesla API configured for user {user.email}")
    return None
