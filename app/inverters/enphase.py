"""Enphase IQ Gateway (Envoy) controller via local REST API.

Supports Enphase microinverter systems with IQ Gateway/Envoy.
Uses DPEL (Device Power Export Limit) for load following curtailment.

Reference: https://github.com/pyenphase/pyenphase
           https://github.com/Matthew1471/Enphase-API
"""
import asyncio
import json
import logging
import ssl
import xml.etree.ElementTree as ET
from typing import Optional
from datetime import datetime, timedelta

import aiohttp

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)


class EnphaseController(InverterController):
    """Controller for Enphase IQ Gateway (Envoy) via local REST API.

    Uses HTTPS to communicate with the IQ Gateway on the local network.
    Requires JWT token authentication for firmware 7.x and above.

    Supports load following curtailment via DPEL (Device Power Export Limit).
    """

    # API endpoints
    ENDPOINT_INFO = "/info"
    ENDPOINT_PRODUCTION = "/api/v1/production"
    ENDPOINT_PRODUCTION_JSON = "/production.json?details=1"
    ENDPOINT_INVERTERS = "/api/v1/production/inverters"
    ENDPOINT_METERS_READINGS = "/ivp/meters/readings"
    ENDPOINT_DPEL = "/ivp/ss/dpel"
    ENDPOINT_DER_SETTINGS = "/ivp/ss/der_settings"
    ENDPOINT_PCS_SETTINGS = "/ivp/ss/pcs_settings"
    ENDPOINT_HOME = "/home.json"

    # AGF (Advanced Grid Functions) endpoints for grid profile switching
    ENDPOINT_AGF_INDEX = "/installer/agf/index.json"
    ENDPOINT_AGF_DETAILS = "/installer/agf/details.json"
    ENDPOINT_AGF_SET_PROFILE = "/installer/agf/set_profile.json"

    # Enlighten cloud endpoints for token retrieval
    ENLIGHTEN_LOGIN_URL = "https://enlighten.enphaseenergy.com/login/login.json"
    ENLIGHTEN_TOKEN_URL = "https://entrez.enphaseenergy.com/tokens"

    # Timeout for HTTP operations
    TIMEOUT_SECONDS = 30.0

    # Token refresh interval (tokens are valid for ~12 hours, refresh at 11)
    TOKEN_REFRESH_HOURS = 11

    def __init__(
        self,
        host: str,
        port: int = 443,
        slave_id: int = 1,  # Not used for Enphase, kept for interface compatibility
        model: Optional[str] = None,
        token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        serial: Optional[str] = None,
        normal_profile: Optional[str] = None,
        zero_export_profile: Optional[str] = None,
    ):
        """Initialize Enphase controller.

        Args:
            host: IP address or hostname of IQ Gateway/Envoy
            port: HTTPS port (default: 443)
            slave_id: Not used for Enphase (interface compatibility)
            model: Envoy model (e.g., 'envoy-s-metered', 'iq-gateway')
            token: JWT token for authentication (if already obtained)
            username: Enlighten username/email (for cloud token retrieval)
            password: Enlighten password (for cloud token retrieval)
            serial: Envoy serial number (for token retrieval, auto-detected if not provided)
            normal_profile: Grid profile name for normal operation (for profile switching fallback)
            zero_export_profile: Grid profile name for zero export (for profile switching fallback)
        """
        super().__init__(host, port, slave_id, model)
        self._token = token
        self._username = username
        self._password = password
        self._serial = serial
        self._normal_profile = normal_profile
        self._zero_export_profile = zero_export_profile
        self._session: Optional[aiohttp.ClientSession] = None
        self._cloud_session: Optional[aiohttp.ClientSession] = None
        self._lock: Optional[asyncio.Lock] = None  # Created lazily in async context
        self._firmware_version: Optional[str] = None
        self._envoy_serial: Optional[str] = None
        self._dpel_supported: Optional[bool] = None
        self._dpel_available: Optional[bool] = None  # None = unknown, True = works, False = broken (503/404)
        self._der_available: Optional[bool] = None   # None = unknown, True = works, False = broken
        self._agf_available: Optional[bool] = None   # None = unknown, True = works, False = broken
        self._profile_switching_supported: Optional[bool] = None
        self._current_profile: Optional[str] = None
        self._token_obtained_at: Optional[datetime] = None
        self._enlighten_session_id: Optional[str] = None

    def _get_lock(self) -> asyncio.Lock:
        """Get or create the asyncio lock (lazy initialization for Flask compatibility)."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _get_enlighten_session(self) -> Optional[str]:
        """Authenticate with Enlighten cloud and get session ID.

        Returns:
            Session ID if successful, None otherwise
        """
        if not self._username or not self._password:
            _LOGGER.debug("No Enlighten credentials provided")
            return None

        try:
            if not self._cloud_session:
                self._cloud_session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30.0)
                )

            login_data = {
                "user[email]": self._username,
                "user[password]": self._password,
            }

            async with self._cloud_session.post(
                self.ENLIGHTEN_LOGIN_URL,
                data=login_data,
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    session_id = result.get("session_id")
                    if session_id:
                        _LOGGER.info("Successfully authenticated with Enlighten cloud")
                        self._enlighten_session_id = session_id
                        return session_id
                    else:
                        _LOGGER.error(f"Enlighten login response missing session_id: {result}")
                else:
                    text = await response.text()
                    _LOGGER.error(f"Enlighten login failed with status {response.status}: {text[:200]}")

        except Exception as e:
            _LOGGER.error(f"Error authenticating with Enlighten: {e}")

        return None

    async def _get_token_from_cloud(self, serial: str) -> Optional[str]:
        """Get JWT token from Enlighten cloud for the specified Envoy.

        Args:
            serial: Envoy serial number

        Returns:
            JWT token if successful, None otherwise
        """
        if not self._enlighten_session_id:
            session_id = await self._get_enlighten_session()
            if not session_id:
                return None
        else:
            session_id = self._enlighten_session_id

        try:
            if not self._cloud_session:
                self._cloud_session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30.0)
                )

            # Request token for this Envoy
            token_data = {
                "session_id": session_id,
                "serial_num": serial,
                "username": self._username,
            }

            async with self._cloud_session.post(
                self.ENLIGHTEN_TOKEN_URL,
                json=token_data,
            ) as response:
                if response.status == 200:
                    token = await response.text()
                    token = token.strip()
                    if token and len(token) > 100:  # JWT tokens are long
                        _LOGGER.info(f"Successfully obtained JWT token from Enlighten for Envoy {serial}")
                        self._token = token
                        self._token_obtained_at = datetime.now()
                        return token
                    else:
                        _LOGGER.error(f"Invalid token response from Enlighten: {token[:100]}")
                else:
                    text = await response.text()
                    _LOGGER.error(f"Enlighten token request failed with status {response.status}: {text[:200]}")

        except Exception as e:
            _LOGGER.error(f"Error getting token from Enlighten: {e}")

        return None

    async def _ensure_token(self, force_refresh: bool = False) -> bool:
        """Ensure we have a valid JWT token, fetching from cloud if needed.

        Args:
            force_refresh: If True, force fetching a new token even if current one seems valid

        Returns:
            True if we have a valid token
        """
        # If we have a token and it's not too old, use it (unless force refresh)
        if self._token and not force_refresh:
            if self._token_obtained_at:
                age = datetime.now() - self._token_obtained_at
                if age < timedelta(hours=self.TOKEN_REFRESH_HOURS):
                    return True
                _LOGGER.info(f"JWT token is {age.total_seconds()/3600:.1f} hours old, refreshing from Enlighten cloud")
            else:
                # Token was provided externally - assume it's valid for the first request
                # If we get a 401, the caller should set force_refresh=True
                if not self._username or not self._password:
                    _LOGGER.debug("External token provided, no credentials for refresh")
                    return True
                # We have credentials but no timestamp - this is the first use of external token
                # Mark when we started using it so we can track age
                self._token_obtained_at = datetime.now()
                _LOGGER.debug("External token provided, marked timestamp for age tracking")
                return True

        # Need to get token from cloud
        if not self._username or not self._password:
            _LOGGER.debug("No Enlighten credentials, cannot fetch token")
            return False

        # Get serial from Envoy if not provided
        serial = self._serial or self._envoy_serial
        if not serial:
            _LOGGER.warning("Cannot fetch token: Envoy serial number not known")
            return False

        _LOGGER.info(f"Fetching new JWT token from Enlighten cloud for Envoy {serial}")
        token = await self._get_token_from_cloud(serial)
        return token is not None

    def _get_ssl_context(self) -> ssl.SSLContext:
        """Get SSL context that accepts self-signed certificates."""
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        return ssl_context

    async def connect(self) -> bool:
        """Connect to the Enphase IQ Gateway."""
        async with self._get_lock():
            try:
                if self._session and not self._session.closed:
                    return True

                # Create connector with SSL context for self-signed certs
                connector = aiohttp.TCPConnector(ssl=self._get_ssl_context())
                timeout = aiohttp.ClientTimeout(total=self.TIMEOUT_SECONDS)
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout,
                )

                # Test connection by getting device info
                info = await self._get_info()
                if info:
                    self._connected = True
                    self._envoy_serial = info.get("serial")
                    self._firmware_version = info.get("software")
                    _LOGGER.info(
                        f"Connected to Enphase IQ Gateway at {self.host} "
                        f"(serial: {self._envoy_serial}, firmware: {self._firmware_version})"
                    )

                    # Fetch JWT token from Enlighten cloud if credentials provided
                    if self._username and self._password and not self._token:
                        serial = self._serial or self._envoy_serial
                        if serial:
                            _LOGGER.info(f"Fetching JWT token from Enlighten cloud for Envoy {serial}")
                            await self._get_token_from_cloud(serial)

                    return True
                else:
                    _LOGGER.error(f"Failed to connect to Enphase IQ Gateway at {self.host}")
                    return False

            except Exception as e:
                _LOGGER.error(f"Error connecting to Enphase IQ Gateway: {e}")
                self._connected = False
                return False

    async def disconnect(self) -> None:
        """Disconnect from the Enphase IQ Gateway."""
        async with self._get_lock():
            if self._session:
                await self._session.close()
                self._session = None
            if self._cloud_session:
                await self._cloud_session.close()
                self._cloud_session = None
            self._connected = False
            _LOGGER.debug(f"Disconnected from Enphase IQ Gateway at {self.host}")

    def _get_headers(self) -> dict:
        """Get HTTP headers with authentication if token is available."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get(self, endpoint: str, retry_auth: bool = True) -> Optional[dict]:
        """Make a GET request to the IQ Gateway."""
        if not self._session:
            if not await self.connect():
                return None

        # Ensure we have a valid token before making authenticated requests
        await self._ensure_token()

        url = f"https://{self.host}:{self.port}{endpoint}"
        try:
            async with self._session.get(url, headers=self._get_headers()) as response:
                if response.status == 200:
                    # Use content_type=None to accept any Content-Type header
                    # Enphase gateways often return incorrect mimetypes
                    return await response.json(content_type=None)
                elif response.status == 401:
                    _LOGGER.debug(f"Authentication required for {endpoint}")
                    # If we got 401 and haven't retried, try refreshing token
                    if retry_auth and self._username and self._password:
                        _LOGGER.info("Got 401, attempting token refresh...")
                        if await self._ensure_token(force_refresh=True):
                            return await self._get(endpoint, retry_auth=False)
                    return None
                else:
                    _LOGGER.debug(f"GET {endpoint} returned status {response.status}")
                    return None

        except aiohttp.ClientError as e:
            _LOGGER.debug(f"HTTP error getting {endpoint}: {e}")
            return None
        except Exception as e:
            _LOGGER.debug(f"Error getting {endpoint}: {e}")
            return None

    async def _put(self, endpoint: str, data: dict, retry_auth: bool = True) -> tuple[bool, Optional[int]]:
        """Make a PUT request to the IQ Gateway.

        Returns:
            Tuple of (success, status_code). status_code is None on network errors.
        """
        if not self._session:
            if not await self.connect():
                return False, None

        # Ensure we have a valid token before making authenticated requests
        await self._ensure_token()

        url = f"https://{self.host}:{self.port}{endpoint}"
        try:
            async with self._session.put(
                url, headers=self._get_headers(), json=data
            ) as response:
                if response.status in (200, 201, 204):
                    _LOGGER.debug(f"PUT {endpoint} successful")
                    return True, response.status
                elif response.status == 401:
                    _LOGGER.error(f"Authentication required for {endpoint}")
                    # If we got 401 and haven't retried, try refreshing token
                    if retry_auth and self._username and self._password:
                        _LOGGER.info("Got 401, attempting token refresh...")
                        if await self._ensure_token(force_refresh=True):
                            return await self._put(endpoint, data, retry_auth=False)
                    return False, 401
                else:
                    _LOGGER.debug(f"PUT {endpoint} returned status {response.status}")
                    return False, response.status

        except aiohttp.ClientError as e:
            _LOGGER.error(f"HTTP error putting {endpoint}: {e}")
            return False, None
        except Exception as e:
            _LOGGER.error(f"Error putting {endpoint}: {e}")
            return False, None

    async def _post(self, endpoint: str, data: dict, retry_auth: bool = True) -> tuple[bool, Optional[int]]:
        """Make a POST request to the IQ Gateway.

        Returns:
            Tuple of (success, status_code). status_code is None on network errors.
        """
        if not self._session:
            if not await self.connect():
                return False, None

        # Ensure we have a valid token before making authenticated requests
        await self._ensure_token()

        url = f"https://{self.host}:{self.port}{endpoint}"
        try:
            async with self._session.post(
                url, headers=self._get_headers(), json=data
            ) as response:
                if response.status in (200, 201, 204):
                    _LOGGER.debug(f"POST {endpoint} successful")
                    return True, response.status
                elif response.status == 401:
                    _LOGGER.error(f"Authentication required for {endpoint}")
                    # If we got 401 and haven't retried, try refreshing token
                    if retry_auth and self._username and self._password:
                        _LOGGER.info("Got 401, attempting token refresh...")
                        if await self._ensure_token(force_refresh=True):
                            return await self._post(endpoint, data, retry_auth=False)
                    return False, 401
                else:
                    # Log response body for debugging 400 errors
                    try:
                        body = await response.text()
                        _LOGGER.debug(f"POST {endpoint} returned status {response.status}: {body[:200]}")
                    except:
                        _LOGGER.debug(f"POST {endpoint} returned status {response.status}")
                    return False, response.status

        except aiohttp.ClientError as e:
            _LOGGER.error(f"HTTP error posting {endpoint}: {e}")
            return False, None
        except Exception as e:
            _LOGGER.error(f"Error posting {endpoint}: {e}")
            return False, None

    async def _get_info(self) -> Optional[dict]:
        """Get device info from the IQ Gateway."""
        # Try info endpoint first - returns XML on most firmware
        try:
            if not self._session:
                if not await self.connect():
                    return None

            url = f"https://{self.host}:{self.port}{self.ENDPOINT_INFO}"
            async with self._session.get(url, headers=self._get_headers()) as response:
                if response.status == 200:
                    text = await response.text()
                    _LOGGER.debug(f"Enphase /info response: {text[:500]}")
                    # Parse XML response
                    try:
                        root = ET.fromstring(text)
                        # Extract device info from XML - search all descendants
                        info = {}
                        for elem in root.iter():
                            # Remove namespace prefix if present
                            tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                            if elem.text and elem.text.strip():
                                info[tag] = elem.text.strip()

                        _LOGGER.debug(f"Enphase parsed info: {info}")

                        if info:
                            # Map common fields from Enphase XML
                            return {
                                "serial": info.get("sn") or info.get("serial") or info.get("device_sn"),
                                "software": info.get("software") or info.get("version") or info.get("imeter_fw_version"),
                                "model": info.get("pn") or info.get("model") or info.get("part_num"),
                            }
                    except ET.ParseError as e:
                        _LOGGER.debug(f"XML parse error: {e}")
                        # Not XML, try JSON
                        try:
                            import json
                            return json.loads(text)
                        except json.JSONDecodeError:
                            _LOGGER.debug(f"Could not parse /info response as XML or JSON")
        except Exception as e:
            _LOGGER.debug(f"Error getting /info: {e}")

        # Try home.json as fallback
        home = await self._get(self.ENDPOINT_HOME)
        if home:
            return {
                "serial": home.get("serial_num"),
                "software": home.get("software_version"),
            }

        return None

    async def _get_production(self) -> Optional[dict]:
        """Get production data from the IQ Gateway."""
        # Try production.json first (more detailed)
        data = await self._get(self.ENDPOINT_PRODUCTION_JSON)
        if data:
            return data

        # Fall back to api/v1/production
        data = await self._get(self.ENDPOINT_PRODUCTION)
        if data:
            return data

        return None

    async def _get_dpel_settings(self) -> Optional[dict]:
        """Get DPEL (Device Power Export Limit) settings."""
        return await self._get(self.ENDPOINT_DPEL)

    async def _set_dpel(self, enabled: bool, limit_watts: int = 0) -> tuple[bool, bool]:
        """Set DPEL (Device Power Export Limit) settings.

        Args:
            enabled: Whether to enable export limiting
            limit_watts: Export limit in watts (0 for zero export)

        Returns:
            Tuple of (success, endpoint_available).
            endpoint_available=False means DPEL returned 503/404 (not supported on this gateway).
        """
        # If we already know DPEL is unavailable, skip it
        if self._dpel_available is False:
            _LOGGER.debug("DPEL previously marked as unavailable, skipping")
            return False, False

        # Firmware D8.x requires 'dynamic_pel_settings' wrapper
        # Try multiple payload formats for maximum compatibility
        payloads = [
            # D8.x firmware format
            {"dynamic_pel_settings": {"enable": 1 if enabled else 0, "limit": limit_watts}},
            # Alternative D8.x format with boolean
            {"dynamic_pel_settings": {"enabled": enabled, "limit": limit_watts}},
            # Older firmware format
            {"enabled": enabled, "limit": limit_watts},
        ]

        for payload in payloads:
            _LOGGER.debug(f"Trying DPEL payload: {payload}")
            success, status = await self._post(self.ENDPOINT_DPEL, payload)

            if success:
                self._dpel_available = True
                return True, True

            # Check if endpoint is not available (503 Service Unavailable, 404 Not Found)
            if status in (503, 404):
                _LOGGER.info(f"DPEL endpoint returned {status} - marking as unavailable (legacy endpoint not supported)")
                self._dpel_available = False
                return False, False

            # 400 errors might be payload format issues, continue trying other formats
            if status == 400:
                _LOGGER.debug(f"DPEL payload rejected with 400, trying next format")
                continue

        # Try PUT as last resort for older firmware
        for payload in payloads:
            success, status = await self._put(self.ENDPOINT_DPEL, payload)
            if success:
                self._dpel_available = True
                return True, True
            if status in (503, 404):
                _LOGGER.info(f"DPEL endpoint returned {status} - marking as unavailable")
                self._dpel_available = False
                return False, False

        # All attempts failed but endpoint exists - might be config issue
        _LOGGER.warning("All DPEL payload formats failed - endpoint exists but rejected all requests")
        return False, True

    async def _get_der_settings(self) -> Optional[dict]:
        """Get DER (Distributed Energy Resource) settings."""
        return await self._get(self.ENDPOINT_DER_SETTINGS)

    async def _set_der_export_limit(self, limit_watts: int) -> tuple[bool, bool]:
        """Set DER export limit.

        Args:
            limit_watts: Export limit in watts (0 for zero export)

        Returns:
            Tuple of (success, endpoint_available).
            endpoint_available=False means DER returned 503/404 or region error.
        """
        # If we already know DER is unavailable, skip it
        if self._der_available is False:
            _LOGGER.debug("DER previously marked as unavailable, skipping")
            return False, False

        # Get current settings first
        current = await self._get_der_settings()
        if not current:
            _LOGGER.debug("Could not get DER settings")
            return False, True  # Endpoint might exist but returned error

        # Check for region error in current settings
        if isinstance(current, dict) and "error" in str(current).lower():
            error_str = str(current)
            if "not valid for" in error_str and "region" in error_str:
                _LOGGER.info(f"DER endpoint returned region error - marking as unavailable: {error_str[:100]}")
                self._der_available = False
                return False, False

        # Update with new export limit
        current["exportLimit"] = limit_watts
        current["exportLimitEnabled"] = limit_watts == 0 or limit_watts > 0

        # Try POST first (required by most firmware), fall back to PUT
        success, status = await self._post(self.ENDPOINT_DER_SETTINGS, current)
        if success:
            self._der_available = True
            return True, True

        if status in (503, 404):
            _LOGGER.info(f"DER endpoint returned {status} - marking as unavailable")
            self._der_available = False
            return False, False

        success, status = await self._put(self.ENDPOINT_DER_SETTINGS, current)
        if success:
            self._der_available = True
            return True, True

        if status in (503, 404):
            _LOGGER.info(f"DER endpoint returned {status} - marking as unavailable")
            self._der_available = False
            return False, False

        return False, True

    # =========================================================================
    # Grid Profile Switching (AGF - Advanced Grid Functions)
    # Fallback method when DPEL/DER endpoints don't work
    # =========================================================================

    async def _get_available_profiles(self) -> Optional[list]:
        """Get list of available grid profiles from the IQ Gateway."""
        data = await self._get(self.ENDPOINT_AGF_INDEX)
        if data and isinstance(data, list):
            _LOGGER.debug(f"Available grid profiles: {data}")
            return data
        return None

    async def _get_current_profile(self) -> Optional[str]:
        """Get the currently active grid profile."""
        data = await self._get(self.ENDPOINT_AGF_DETAILS)
        if data:
            profile_name = data.get("selected_profile") or data.get("profile_name") or data.get("name")
            if profile_name:
                self._current_profile = profile_name
                _LOGGER.debug(f"Current grid profile: {profile_name}")
                return profile_name
        return None

    async def _auto_detect_profiles(self) -> tuple[Optional[str], Optional[str]]:
        """Auto-detect zero export and normal grid profiles from available profiles.

        Looks for profiles matching common patterns:
        - Zero export: "0 kW Export", "Zero Export", "No Export"
        - Normal: "5 kW Export", "10 kW Export", or any non-zero export limit

        Returns:
            Tuple of (zero_export_profile, normal_profile). Either may be None if not found.
        """
        profiles = await self._get_available_profiles()
        if not profiles:
            _LOGGER.debug("No profiles available for auto-detection")
            return None, None

        _LOGGER.info(f"Auto-detecting profiles from {len(profiles)} available: {profiles}")

        zero_export_profile = None
        normal_profile = None
        current = await self._get_current_profile()

        for profile in profiles:
            if not isinstance(profile, str):
                continue

            profile_lower = profile.lower()

            # Detect zero export profiles
            if any(pattern in profile_lower for pattern in ["0 kw export", "zero export", "no export", "0kw export"]):
                zero_export_profile = profile
                _LOGGER.info(f"Auto-detected zero export profile: {profile}")

            # Detect normal export profiles (non-zero export limits)
            elif any(pattern in profile_lower for pattern in ["5 kw export", "10 kw export", "export limit"]):
                # Make sure it's not zero export
                if "0 kw" not in profile_lower and "zero" not in profile_lower:
                    normal_profile = profile
                    _LOGGER.info(f"Auto-detected normal export profile: {profile}")

        # If we didn't find a normal profile but have a current profile that's not zero-export, use it
        if not normal_profile and current:
            current_lower = current.lower()
            if not any(pattern in current_lower for pattern in ["0 kw export", "zero export", "no export"]):
                normal_profile = current
                _LOGGER.info(f"Using current profile as normal profile: {current}")

        return zero_export_profile, normal_profile

    async def _ensure_profiles_configured(self) -> bool:
        """Ensure we have zero export and normal profiles configured.

        If not manually configured, attempts to auto-detect them from available profiles.

        Returns:
            True if both profiles are available (configured or auto-detected)
        """
        if self._zero_export_profile and self._normal_profile:
            return True

        # Try to auto-detect missing profiles
        zero_export, normal = await self._auto_detect_profiles()

        if not self._zero_export_profile and zero_export:
            self._zero_export_profile = zero_export
            _LOGGER.info(f"Auto-configured zero export profile: {zero_export}")

        if not self._normal_profile and normal:
            self._normal_profile = normal
            _LOGGER.info(f"Auto-configured normal profile: {normal}")

        # Log what we have
        if self._zero_export_profile and self._normal_profile:
            _LOGGER.info(
                f"AGF profiles ready - zero_export: '{self._zero_export_profile}', "
                f"normal: '{self._normal_profile}'"
            )
            return True
        else:
            missing = []
            if not self._zero_export_profile:
                missing.append("zero_export_profile")
            if not self._normal_profile:
                missing.append("normal_profile")
            _LOGGER.warning(f"Could not auto-detect profiles: {', '.join(missing)} not found")
            return False

    async def _set_grid_profile(self, profile_name: str) -> tuple[bool, bool]:
        """Set the active grid profile via AGF endpoint.

        Returns:
            Tuple of (success, endpoint_available).
            endpoint_available=False means AGF returned 503/404.
        """
        if not profile_name:
            _LOGGER.error("Cannot set grid profile: no profile name provided")
            return False, True

        # If we already know AGF is unavailable, skip it
        if self._agf_available is False:
            _LOGGER.debug("AGF previously marked as unavailable, skipping")
            return False, False

        _LOGGER.info(f"Setting grid profile to: {profile_name}")
        data = {"selected_profile": profile_name}

        success, status = await self._put(self.ENDPOINT_AGF_SET_PROFILE, data)
        if success:
            self._current_profile = profile_name
            self._profile_switching_supported = True
            self._agf_available = True
            _LOGGER.info(f"Successfully set grid profile to: {profile_name}")
            return True, True

        if status in (503, 404):
            _LOGGER.info(f"AGF endpoint returned {status} - marking as unavailable")
            self._agf_available = False
            return False, False

        success, status = await self._post(self.ENDPOINT_AGF_SET_PROFILE, data)
        if success:
            self._current_profile = profile_name
            self._profile_switching_supported = True
            self._agf_available = True
            _LOGGER.info(f"Successfully set grid profile to: {profile_name}")
            return True, True

        if status in (503, 404):
            _LOGGER.info(f"AGF endpoint returned {status} - marking as unavailable")
            self._agf_available = False
            return False, False

        _LOGGER.error(f"Failed to set grid profile to: {profile_name}")
        return False, True

    async def _switch_to_zero_export_profile(self) -> tuple[bool, bool]:
        """Switch to zero export grid profile.

        Returns:
            Tuple of (success, endpoint_available).
        """
        if not self._zero_export_profile:
            _LOGGER.debug("No zero export profile configured")
            return False, True  # Not a failure of the endpoint, just not configured

        if not self._current_profile:
            current = await self._get_current_profile()
            if current and current != self._zero_export_profile:
                if not self._normal_profile:
                    self._normal_profile = current
                    _LOGGER.info(f"Auto-detected normal profile: {current}")

        return await self._set_grid_profile(self._zero_export_profile)

    async def _switch_to_normal_profile(self) -> tuple[bool, bool]:
        """Switch to normal (non-zero-export) grid profile.

        Returns:
            Tuple of (success, endpoint_available).
        """
        if not self._normal_profile:
            _LOGGER.debug("No normal profile configured")
            return False, True  # Not a failure of the endpoint, just not configured
        return await self._set_grid_profile(self._normal_profile)

    async def curtail(self) -> bool:
        """Enable load following curtailment on the Enphase system.

        Tries methods in order: DPEL, DER settings, then AGF grid profile switching.
        Caches endpoint availability to skip known-broken endpoints on subsequent calls.

        Returns:
            True if curtailment successful
        """
        _LOGGER.info(f"Curtailing Enphase system at {self.host} (zero export mode)")

        try:
            if not await self.connect():
                _LOGGER.error("Cannot curtail: failed to connect to IQ Gateway")
                return False

            # Method 1: Try DPEL endpoint first (fastest, most dynamic) - for backward compatibility
            if self._dpel_available is not False:
                _LOGGER.debug("Trying DPEL endpoint for curtailment")
                success, available = await self._set_dpel(enabled=True, limit_watts=0)
                if success:
                    _LOGGER.info(f"Successfully curtailed Enphase system at {self.host} via DPEL")
                    self._dpel_supported = True
                    await asyncio.sleep(1)
                    return True
                if not available:
                    _LOGGER.info("DPEL endpoint not available on this gateway (503/404), will use fallback methods")
            else:
                _LOGGER.debug("DPEL known to be unavailable, skipping")

            # Method 2: Try DER settings as second option
            if self._der_available is not False:
                _LOGGER.debug("Trying DER settings for curtailment")
                success, available = await self._set_der_export_limit(0)
                if success:
                    _LOGGER.info(f"Successfully curtailed Enphase system at {self.host} via DER")
                    await asyncio.sleep(1)
                    return True
                if not available:
                    _LOGGER.info("DER endpoint not available (503/404 or region error), will use AGF profile switching")
            else:
                _LOGGER.debug("DER known to be unavailable, skipping")

            # Method 3: AGF Grid profile switching
            # This is the modern replacement for DPEL and works on most recent firmware
            # Auto-detect profiles if not manually configured
            if not self._zero_export_profile:
                _LOGGER.debug("No zero export profile configured, attempting auto-detection")
                await self._ensure_profiles_configured()

            if self._zero_export_profile:
                _LOGGER.debug(f"Trying AGF profile switching to zero export profile: {self._zero_export_profile}")
                success, available = await self._switch_to_zero_export_profile()
                if success:
                    _LOGGER.info(
                        f"Successfully curtailed Enphase system at {self.host} via AGF profile switching "
                        f"to '{self._zero_export_profile}'. Note: May take 30-60 seconds to propagate to microinverters."
                    )
                    await asyncio.sleep(5)
                    return True
                if not available:
                    _LOGGER.warning("AGF endpoint also not available - no curtailment methods work on this gateway")
            else:
                _LOGGER.debug("No zero export profile available (manual or auto-detected) for AGF fallback")

            # All methods failed
            methods_tried = []
            if self._dpel_available is not False:
                methods_tried.append("DPEL")
            if self._der_available is not False:
                methods_tried.append("DER")
            if self._zero_export_profile:
                methods_tried.append("AGF")

            _LOGGER.warning(
                f"Export limiting not available on this Enphase system. "
                f"Tried: {', '.join(methods_tried) if methods_tried else 'none'}. "
                f"DPEL available: {self._dpel_available}, DER available: {self._der_available}, "
                f"AGF available: {self._agf_available}. "
                f"Configure zero_export_profile for profile switching fallback."
            )
            return False

        except Exception as e:
            _LOGGER.error(f"Error curtailing Enphase system: {e}")
            return False

    async def restore(self) -> bool:
        """Restore normal operation of the Enphase system.

        Tries methods in order: DPEL, DER settings, then AGF grid profile switching.
        Caches endpoint availability to skip known-broken endpoints on subsequent calls.

        Returns:
            True if restore successful
        """
        _LOGGER.info(f"Restoring Enphase system at {self.host} to normal operation")

        try:
            if not await self.connect():
                _LOGGER.error("Cannot restore: failed to connect to IQ Gateway")
                return False

            # Method 1: Try DPEL endpoint first (fastest) - for backward compatibility
            if self._dpel_available is not False:
                _LOGGER.debug("Trying DPEL endpoint for restore")
                success, available = await self._set_dpel(enabled=False, limit_watts=0)
                if success:
                    _LOGGER.info(f"Successfully restored Enphase system at {self.host} via DPEL")
                    await asyncio.sleep(1)
                    return True
                if not available:
                    _LOGGER.info("DPEL endpoint not available on this gateway, will use fallback methods")
            else:
                _LOGGER.debug("DPEL known to be unavailable, skipping")

            # Method 2: Try DER settings (set high limit to effectively disable)
            if self._der_available is not False:
                _LOGGER.debug("Trying DER settings for restore")
                success, available = await self._set_der_export_limit(100000)  # 100kW effectively unlimited
                if success:
                    _LOGGER.info(f"Successfully restored Enphase system at {self.host} via DER")
                    await asyncio.sleep(1)
                    return True
                if not available:
                    _LOGGER.info("DER endpoint not available, will use AGF profile switching")
            else:
                _LOGGER.debug("DER known to be unavailable, skipping")

            # Method 3: AGF Grid profile switching
            # Auto-detect profiles if not manually configured
            if not self._normal_profile:
                _LOGGER.debug("No normal profile configured, attempting auto-detection")
                await self._ensure_profiles_configured()

            if self._normal_profile:
                _LOGGER.debug(f"Trying AGF profile switching to normal profile: {self._normal_profile}")
                success, available = await self._switch_to_normal_profile()
                if success:
                    _LOGGER.info(
                        f"Successfully restored Enphase system at {self.host} via AGF profile switching "
                        f"to '{self._normal_profile}'. Note: May take 30-60 seconds to propagate to microinverters."
                    )
                    await asyncio.sleep(5)
                    return True
                if not available:
                    _LOGGER.warning("AGF endpoint also not available - no restore methods work on this gateway")
            else:
                _LOGGER.debug("No normal profile available (manual or auto-detected) for AGF fallback")

            # All methods failed
            _LOGGER.warning(
                f"Failed to restore normal operation. "
                f"DPEL available: {self._dpel_available}, DER available: {self._der_available}, "
                f"AGF available: {self._agf_available}. "
                f"Configure normal_profile for profile switching fallback."
            )
            return False

        except Exception as e:
            _LOGGER.error(f"Error restoring Enphase system: {e}")
            return False

    async def _read_all_data(self) -> dict:
        """Read all available data and return as attributes dict."""
        attrs = {}

        try:
            # Get production data
            production = await self._get_production()
            if production:
                # Handle production.json format
                if "production" in production:
                    prod_list = production.get("production", [])
                    for item in prod_list:
                        if item.get("type") == "inverters":
                            attrs["inverters_active"] = item.get("activeCount", 0)
                            attrs["production_w"] = item.get("wNow", 0)
                            attrs["daily_production_wh"] = item.get("whToday", 0)
                            attrs["lifetime_production_wh"] = item.get("whLifetime", 0)
                        elif item.get("type") == "eim":
                            attrs["production_w"] = item.get("wNow", 0)
                            attrs["daily_production_wh"] = item.get("whToday", 0)

                    consumption = production.get("consumption", [])
                    for item in consumption:
                        if item.get("measurementType") == "total-consumption":
                            attrs["consumption_w"] = item.get("wNow", 0)
                            attrs["daily_consumption_wh"] = item.get("whToday", 0)
                        elif item.get("measurementType") == "net-consumption":
                            attrs["net_consumption_w"] = item.get("wNow", 0)
                            # Positive = importing, negative = exporting

                # Handle api/v1/production format
                elif "wattsNow" in production:
                    attrs["production_w"] = production.get("wattsNow", 0)
                    attrs["daily_production_wh"] = production.get("wattHoursToday", 0)
                    attrs["lifetime_production_wh"] = production.get("wattHoursLifetime", 0)

            # Get inverter count
            inverters = await self._get(self.ENDPOINT_INVERTERS)
            if inverters and isinstance(inverters, list):
                attrs["inverter_count"] = len(inverters)
                total_max_power = sum(inv.get("maxReportWatts", 0) for inv in inverters)
                attrs["system_capacity_w"] = total_max_power

            # Get DPEL settings
            dpel = await self._get_dpel_settings()
            if dpel:
                attrs["dpel_enabled"] = dpel.get("enabled", False)
                attrs["dpel_limit_w"] = dpel.get("limit", 0)
                self._dpel_supported = True
            else:
                self._dpel_supported = False

            # Get meter readings if available
            meters = await self._get(self.ENDPOINT_METERS_READINGS)
            if meters and isinstance(meters, list):
                for meter in meters:
                    eid = meter.get("eid")
                    if meter.get("measurementType") == "production":
                        attrs["meter_production_w"] = meter.get("activePower", 0)
                    elif meter.get("measurementType") == "net-consumption":
                        attrs["meter_grid_w"] = meter.get("activePower", 0)

        except Exception as e:
            _LOGGER.warning(f"Error reading some data: {e}")

        return attrs

    async def get_status(self) -> InverterState:
        """Get current status of the Enphase system.

        Returns:
            InverterState with current status and data attributes
        """
        try:
            if not await self.connect():
                return InverterState(
                    status=InverterStatus.OFFLINE,
                    is_curtailed=False,
                    error_message="Failed to connect to IQ Gateway",
                )

            # Read all available data
            attrs = await self._read_all_data()

            # Determine status
            status = InverterStatus.ONLINE
            is_curtailed = False

            # Check production
            production_w = attrs.get("production_w", 0)
            if production_w is None or production_w == 0:
                # Could be night time or curtailed
                attrs["running_state"] = "idle"
            else:
                attrs["running_state"] = "producing"

            # Check if DPEL is active (curtailed)
            if attrs.get("dpel_enabled") and attrs.get("dpel_limit_w", 10000) == 0:
                is_curtailed = True
                attrs["running_state"] = "export_limited"
                status = InverterStatus.CURTAILED

            # Add device info
            attrs["model"] = self.model or "IQ Gateway"
            attrs["host"] = self.host
            if self._envoy_serial:
                attrs["serial"] = self._envoy_serial
            if self._firmware_version:
                attrs["firmware"] = self._firmware_version
            attrs["dpel_supported"] = self._dpel_supported

            self._last_state = InverterState(
                status=status,
                is_curtailed=is_curtailed,
                power_output_w=float(production_w) if production_w else None,
                attributes=attrs,
            )

            return self._last_state

        except Exception as e:
            _LOGGER.error(f"Error getting Enphase system status: {e}")
            return InverterState(
                status=InverterStatus.ERROR,
                is_curtailed=False,
                error_message=str(e),
            )

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.disconnect()
