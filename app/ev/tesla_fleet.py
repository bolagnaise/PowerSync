# app/ev/tesla_fleet.py
"""
Tesla Fleet API client for EV charging control.

Implements OAuth2 authentication and vehicle control endpoints for:
- Listing vehicles
- Getting vehicle data (charge state, battery level, etc.)
- Starting/stopping charging
- Setting charge limit and amps

Reference: https://developer.tesla.com/docs/fleet-api
"""

import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlencode

_LOGGER = logging.getLogger(__name__)

# Tesla Fleet API endpoints
TESLA_AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TESLA_TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
TESLA_API_BASE = "https://fleet-api.prd.na.vn.cloud.tesla.com"  # North America region

# Required OAuth scopes for EV charging control
REQUIRED_SCOPES = [
    "openid",
    "offline_access",
    "user_data",
    "vehicle_device_data",
    "vehicle_cmds",
    "vehicle_charging_cmds",
]


class TeslaFleetError(Exception):
    """Base exception for Tesla Fleet API errors."""
    pass


class TeslaAuthError(TeslaFleetError):
    """Authentication error with Tesla Fleet API."""
    pass


class TeslaVehicleError(TeslaFleetError):
    """Error with vehicle operations."""
    pass


class TeslaFleetClient:
    """
    Tesla Fleet API client for EV operations.

    Handles OAuth2 authentication, token refresh, and vehicle commands.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        token_expires_at: Optional[datetime] = None,
    ):
        """
        Initialize the Tesla Fleet API client.

        Args:
            client_id: Tesla Fleet API client ID
            client_secret: Tesla Fleet API client secret
            redirect_uri: OAuth redirect URI
            access_token: Existing access token (optional)
            refresh_token: Existing refresh token (optional)
            token_expires_at: Token expiration datetime (optional)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expires_at = token_expires_at
        self._session = requests.Session()

    def get_authorization_url(self, state: str) -> str:
        """
        Generate the OAuth authorization URL for user consent.

        Args:
            state: Random state value for CSRF protection

        Returns:
            Authorization URL to redirect user to
        """
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(REQUIRED_SCOPES),
            "state": state,
        }
        return f"{TESLA_AUTH_URL}?{urlencode(params)}"

    def exchange_code_for_token(self, code: str) -> Dict[str, Any]:
        """
        Exchange authorization code for access and refresh tokens.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            Token response containing access_token, refresh_token, expires_in
        """
        data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "audience": TESLA_API_BASE,
        }

        response = self._session.post(
            TESLA_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code != 200:
            _LOGGER.error(f"Token exchange failed: {response.status_code} {response.text}")
            raise TeslaAuthError(f"Token exchange failed: {response.text}")

        token_data = response.json()
        self._update_tokens(token_data)
        return token_data

    def refresh_access_token(self) -> Dict[str, Any]:
        """
        Refresh the access token using the refresh token.

        Returns:
            Token response containing new access_token and refresh_token
        """
        if not self.refresh_token:
            raise TeslaAuthError("No refresh token available")

        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }

        response = self._session.post(
            TESLA_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code != 200:
            _LOGGER.error(f"Token refresh failed: {response.status_code} {response.text}")
            raise TeslaAuthError(f"Token refresh failed: {response.text}")

        token_data = response.json()
        self._update_tokens(token_data)
        return token_data

    def _update_tokens(self, token_data: Dict[str, Any]):
        """Update stored tokens from response."""
        self.access_token = token_data.get("access_token")
        self.refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 3600)
        self.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    def _ensure_valid_token(self):
        """Ensure we have a valid access token, refreshing if needed."""
        if not self.access_token:
            raise TeslaAuthError("No access token available")

        # Refresh if token expires within 5 minutes
        if self.token_expires_at and datetime.utcnow() > self.token_expires_at - timedelta(minutes=5):
            _LOGGER.info("Access token expiring soon, refreshing...")
            self.refresh_access_token()

    def _api_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Make an authenticated API request.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (e.g., "/api/1/vehicles")
            data: Request body data (for POST)
            params: Query parameters (for GET)

        Returns:
            API response data
        """
        self._ensure_valid_token()

        url = f"{TESLA_API_BASE}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        response = self._session.request(
            method=method,
            url=url,
            headers=headers,
            json=data,
            params=params,
        )

        if response.status_code == 401:
            # Token may have expired, try refreshing
            _LOGGER.info("Got 401, attempting token refresh...")
            self.refresh_access_token()
            headers["Authorization"] = f"Bearer {self.access_token}"
            response = self._session.request(
                method=method,
                url=url,
                headers=headers,
                json=data,
                params=params,
            )

        if response.status_code not in (200, 201):
            _LOGGER.error(f"API request failed: {response.status_code} {response.text}")
            raise TeslaFleetError(f"API request failed: {response.text}")

        return response.json()

    # =========================================================================
    # Vehicle List and Data
    # =========================================================================

    def list_vehicles(self) -> List[Dict[str, Any]]:
        """
        Get list of vehicles associated with the account.

        Returns:
            List of vehicle dictionaries
        """
        result = self._api_request("GET", "/api/1/vehicles")
        return result.get("response", [])

    def get_vehicle_data(self, vehicle_id: str) -> Dict[str, Any]:
        """
        Get detailed vehicle data including charge state.

        Args:
            vehicle_id: Tesla vehicle ID

        Returns:
            Vehicle data dictionary
        """
        result = self._api_request("GET", f"/api/1/vehicles/{vehicle_id}/vehicle_data")
        return result.get("response", {})

    def wake_up_vehicle(self, vehicle_id: str) -> Dict[str, Any]:
        """
        Wake up a sleeping vehicle.

        Args:
            vehicle_id: Tesla vehicle ID

        Returns:
            Wake up response
        """
        result = self._api_request("POST", f"/api/1/vehicles/{vehicle_id}/wake_up")
        return result.get("response", {})

    # =========================================================================
    # Charging Commands
    # =========================================================================

    def charge_start(self, vehicle_id: str) -> Dict[str, Any]:
        """
        Start charging the vehicle.

        Args:
            vehicle_id: Tesla vehicle ID

        Returns:
            Command response
        """
        result = self._api_request("POST", f"/api/1/vehicles/{vehicle_id}/command/charge_start")
        return result.get("response", {})

    def charge_stop(self, vehicle_id: str) -> Dict[str, Any]:
        """
        Stop charging the vehicle.

        Args:
            vehicle_id: Tesla vehicle ID

        Returns:
            Command response
        """
        result = self._api_request("POST", f"/api/1/vehicles/{vehicle_id}/command/charge_stop")
        return result.get("response", {})

    def set_charge_limit(self, vehicle_id: str, percent: int) -> Dict[str, Any]:
        """
        Set the charge limit percentage.

        Args:
            vehicle_id: Tesla vehicle ID
            percent: Charge limit (50-100)

        Returns:
            Command response
        """
        percent = max(50, min(100, percent))
        result = self._api_request(
            "POST",
            f"/api/1/vehicles/{vehicle_id}/command/set_charge_limit",
            data={"percent": percent}
        )
        return result.get("response", {})

    def set_charging_amps(self, vehicle_id: str, amps: int) -> Dict[str, Any]:
        """
        Set the charging amperage.

        Args:
            vehicle_id: Tesla vehicle ID
            amps: Charging amps (typically 1-48)

        Returns:
            Command response
        """
        result = self._api_request(
            "POST",
            f"/api/1/vehicles/{vehicle_id}/command/set_charging_amps",
            data={"charging_amps": amps}
        )
        return result.get("response", {})

    def charge_port_door_open(self, vehicle_id: str) -> Dict[str, Any]:
        """
        Open the charge port door.

        Args:
            vehicle_id: Tesla vehicle ID

        Returns:
            Command response
        """
        result = self._api_request(
            "POST",
            f"/api/1/vehicles/{vehicle_id}/command/charge_port_door_open"
        )
        return result.get("response", {})

    def charge_port_door_close(self, vehicle_id: str) -> Dict[str, Any]:
        """
        Close the charge port door.

        Args:
            vehicle_id: Tesla vehicle ID

        Returns:
            Command response
        """
        result = self._api_request(
            "POST",
            f"/api/1/vehicles/{vehicle_id}/command/charge_port_door_close"
        )
        return result.get("response", {})


def get_fleet_client_for_user(user) -> Optional[TeslaFleetClient]:
    """
    Get a Tesla Fleet API client for a user.

    Args:
        user: User model instance

    Returns:
        TeslaFleetClient instance or None if not configured
    """
    from app.utils.encryption import decrypt_data

    # Check if Fleet API credentials are configured
    if not user.fleet_api_client_id_encrypted:
        return None

    try:
        client_id = decrypt_data(user.fleet_api_client_id_encrypted)
        client_secret = decrypt_data(user.fleet_api_client_secret_encrypted) if user.fleet_api_client_secret_encrypted else None
        access_token = decrypt_data(user.fleet_api_access_token_encrypted) if user.fleet_api_access_token_encrypted else None
        refresh_token = decrypt_data(user.fleet_api_refresh_token_encrypted) if user.fleet_api_refresh_token_encrypted else None

        return TeslaFleetClient(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=user.fleet_api_redirect_uri or "",
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=user.fleet_api_token_expires_at,
        )
    except Exception as e:
        _LOGGER.error(f"Error creating Fleet API client: {e}")
        return None


def sync_vehicles_for_user(user) -> Tuple[int, List[str]]:
    """
    Sync Tesla vehicles for a user from Fleet API.

    Args:
        user: User model instance

    Returns:
        Tuple of (number synced, list of errors)
    """
    from app import db
    from app.models import TeslaVehicle

    client = get_fleet_client_for_user(user)
    if not client:
        return 0, ["Fleet API not configured"]

    errors = []
    synced = 0

    try:
        vehicles = client.list_vehicles()

        for v in vehicles:
            vehicle_id = str(v.get("id"))

            # Find or create vehicle record
            vehicle = TeslaVehicle.query.filter_by(
                user_id=user.id,
                vehicle_id=vehicle_id
            ).first()

            if not vehicle:
                vehicle = TeslaVehicle(user_id=user.id, vehicle_id=vehicle_id)
                db.session.add(vehicle)

            # Update basic info
            vehicle.vin = v.get("vin")
            vehicle.display_name = v.get("display_name")
            vehicle.is_online = v.get("state") == "online"

            # Try to get detailed data if vehicle is online
            if vehicle.is_online:
                try:
                    data = client.get_vehicle_data(vehicle_id)
                    _update_vehicle_from_data(vehicle, data)
                except Exception as e:
                    _LOGGER.warning(f"Could not get data for vehicle {vehicle_id}: {e}")

            synced += 1

        db.session.commit()

    except TeslaAuthError as e:
        errors.append(f"Authentication error: {e}")
    except TeslaFleetError as e:
        errors.append(f"API error: {e}")
    except Exception as e:
        errors.append(f"Unexpected error: {e}")
        db.session.rollback()

    return synced, errors


def _update_vehicle_from_data(vehicle, data: Dict[str, Any]):
    """Update vehicle record from API response data."""
    # Vehicle config
    vehicle_config = data.get("vehicle_config", {})
    vehicle.model = vehicle_config.get("car_type", "").replace("model", "Model ").strip()
    vehicle.year = vehicle_config.get("model_year")
    vehicle.color = vehicle_config.get("exterior_color")

    # Charge state
    charge_state = data.get("charge_state", {})
    vehicle.charging_state = charge_state.get("charging_state")
    vehicle.battery_level = charge_state.get("battery_level")
    vehicle.battery_range = charge_state.get("battery_range")
    vehicle.charge_limit_soc = charge_state.get("charge_limit_soc")
    vehicle.charge_current_request = charge_state.get("charge_current_request")
    vehicle.charge_current_actual = charge_state.get("charger_actual_current")
    vehicle.charger_voltage = charge_state.get("charger_voltage")
    vehicle.charger_power = charge_state.get("charger_power")
    vehicle.time_to_full_charge = charge_state.get("time_to_full_charge")
    vehicle.charge_port_door_open = charge_state.get("charge_port_door_open")
    vehicle.charge_port_latch = charge_state.get("charge_port_latch")
    vehicle.is_plugged_in = charge_state.get("charging_state") not in (None, "Disconnected")

    # Drive state (location)
    drive_state = data.get("drive_state", {})
    vehicle.latitude = drive_state.get("latitude")
    vehicle.longitude = drive_state.get("longitude")

    # Timestamps
    vehicle.last_seen = datetime.utcnow()
    vehicle.data_updated_at = datetime.utcnow()
