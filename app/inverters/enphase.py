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
        """
        super().__init__(host, port, slave_id, model)
        self._token = token
        self._username = username
        self._password = password
        self._serial = serial
        self._session: Optional[aiohttp.ClientSession] = None
        self._cloud_session: Optional[aiohttp.ClientSession] = None
        self._lock: Optional[asyncio.Lock] = None  # Created lazily in async context
        self._firmware_version: Optional[str] = None
        self._envoy_serial: Optional[str] = None
        self._dpel_supported: Optional[bool] = None
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

    async def _put(self, endpoint: str, data: dict, retry_auth: bool = True) -> bool:
        """Make a PUT request to the IQ Gateway."""
        if not self._session:
            if not await self.connect():
                return False

        # Ensure we have a valid token before making authenticated requests
        await self._ensure_token()

        url = f"https://{self.host}:{self.port}{endpoint}"
        try:
            async with self._session.put(
                url, headers=self._get_headers(), json=data
            ) as response:
                if response.status in (200, 201, 204):
                    _LOGGER.debug(f"PUT {endpoint} successful")
                    return True
                elif response.status == 401:
                    _LOGGER.error(f"Authentication required for {endpoint}")
                    # If we got 401 and haven't retried, try refreshing token
                    if retry_auth and self._username and self._password:
                        _LOGGER.info("Got 401, attempting token refresh...")
                        if await self._ensure_token(force_refresh=True):
                            return await self._put(endpoint, data, retry_auth=False)
                    return False
                else:
                    _LOGGER.debug(f"PUT {endpoint} returned status {response.status}")
                    return False

        except aiohttp.ClientError as e:
            _LOGGER.error(f"HTTP error putting {endpoint}: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Error putting {endpoint}: {e}")
            return False

    async def _post(self, endpoint: str, data: dict, retry_auth: bool = True) -> bool:
        """Make a POST request to the IQ Gateway."""
        if not self._session:
            if not await self.connect():
                return False

        # Ensure we have a valid token before making authenticated requests
        await self._ensure_token()

        url = f"https://{self.host}:{self.port}{endpoint}"
        try:
            async with self._session.post(
                url, headers=self._get_headers(), json=data
            ) as response:
                if response.status in (200, 201, 204):
                    _LOGGER.debug(f"POST {endpoint} successful")
                    return True
                elif response.status == 401:
                    _LOGGER.error(f"Authentication required for {endpoint}")
                    # If we got 401 and haven't retried, try refreshing token
                    if retry_auth and self._username and self._password:
                        _LOGGER.info("Got 401, attempting token refresh...")
                        if await self._ensure_token(force_refresh=True):
                            return await self._post(endpoint, data, retry_auth=False)
                    return False
                else:
                    # Log response body for debugging 400 errors
                    try:
                        body = await response.text()
                        _LOGGER.debug(f"POST {endpoint} returned status {response.status}: {body[:200]}")
                    except:
                        _LOGGER.debug(f"POST {endpoint} returned status {response.status}")
                    return False

        except aiohttp.ClientError as e:
            _LOGGER.error(f"HTTP error posting {endpoint}: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Error posting {endpoint}: {e}")
            return False

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

    async def _set_dpel(self, enabled: bool, limit_watts: int = 0) -> bool:
        """Set DPEL (Device Power Export Limit) settings.

        Args:
            enabled: Whether to enable export limiting
            limit_watts: Export limit in watts (0 for zero export)

        Returns:
            True if successful
        """
        # Firmware D8.x requires 'dynamic_pel_settings' wrapper
        data = {
            "dynamic_pel_settings": {
                "enabled": enabled,
                "limit": limit_watts,
            }
        }
        # Try POST first (required by most firmware), fall back to PUT
        if await self._post(self.ENDPOINT_DPEL, data):
            return True

        # Try without wrapper for older firmware
        data_simple = {
            "enabled": enabled,
            "limit": limit_watts,
        }
        return await self._put(self.ENDPOINT_DPEL, data_simple)

    async def _get_der_settings(self) -> Optional[dict]:
        """Get DER (Distributed Energy Resource) settings."""
        return await self._get(self.ENDPOINT_DER_SETTINGS)

    async def _set_der_export_limit(self, limit_watts: int) -> bool:
        """Set DER export limit.

        Args:
            limit_watts: Export limit in watts (0 for zero export)

        Returns:
            True if successful
        """
        # Get current settings first
        current = await self._get_der_settings()
        if not current:
            return False

        # Update with new export limit
        current["exportLimit"] = limit_watts
        current["exportLimitEnabled"] = limit_watts == 0 or limit_watts > 0

        # Try POST first (required by most firmware), fall back to PUT
        if await self._post(self.ENDPOINT_DER_SETTINGS, current):
            return True
        return await self._put(self.ENDPOINT_DER_SETTINGS, current)

    async def curtail(self) -> bool:
        """Enable load following curtailment on the Enphase system.

        Sets export limit to 0W via DPEL or DER settings.

        Returns:
            True if curtailment successful
        """
        _LOGGER.info(f"Curtailing Enphase system at {self.host} (zero export mode)")

        try:
            if not await self.connect():
                _LOGGER.error("Cannot curtail: failed to connect to IQ Gateway")
                return False

            # Try DPEL endpoint first
            success = await self._set_dpel(enabled=True, limit_watts=0)
            if success:
                _LOGGER.info(f"Successfully curtailed Enphase system at {self.host} via DPEL")
                self._dpel_supported = True
                await asyncio.sleep(1)
                return True

            _LOGGER.debug("DPEL not available, trying DER settings")

            # Try DER settings as fallback
            success = await self._set_der_export_limit(0)
            if success:
                _LOGGER.info(f"Successfully curtailed Enphase system at {self.host} via DER")
                await asyncio.sleep(1)
                return True

            _LOGGER.warning(
                "Export limiting not available on this Enphase system. "
                "This may require installer-level access or a specific grid profile."
            )
            return False

        except Exception as e:
            _LOGGER.error(f"Error curtailing Enphase system: {e}")
            return False

    async def restore(self) -> bool:
        """Restore normal operation of the Enphase system.

        Disables export limiting to return to normal export behavior.

        Returns:
            True if restore successful
        """
        _LOGGER.info(f"Restoring Enphase system at {self.host} to normal operation")

        try:
            if not await self.connect():
                _LOGGER.error("Cannot restore: failed to connect to IQ Gateway")
                return False

            # Try DPEL endpoint first
            success = await self._set_dpel(enabled=False, limit_watts=0)
            if success:
                _LOGGER.info(f"Successfully restored Enphase system at {self.host} via DPEL")
                await asyncio.sleep(1)
                return True

            # Try DER settings as fallback (set high limit to effectively disable)
            success = await self._set_der_export_limit(100000)  # 100kW effectively unlimited
            if success:
                _LOGGER.info(f"Successfully restored Enphase system at {self.host} via DER")
                await asyncio.sleep(1)
                return True

            _LOGGER.warning("Failed to restore normal operation")
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
