"""HTTP views exposing Powerwall local pairing + control to the mobile app.

Endpoints:
    POST /api/power_sync/powerwall/pair/start    - begin pairing attempt
    GET  /api/power_sync/powerwall/pair/status   - poll current pairing status
    POST /api/power_sync/powerwall/pair/cancel   - abort in-flight pairing
    POST /api/power_sync/powerwall/pair/unpair   - clear stored key + state
    POST /api/power_sync/powerwall/off_grid      - go off-grid / reconnect
    GET  /api/power_sync/powerwall/local_status  - live local snapshot

Every view requires the mobile app's long-lived HA bearer token
(``requires_auth = True``). The pair/start endpoint also accepts the
gateway IP + customer password + WiFi credentials from the app, mirrors
them into the config entry so they persist device-independently, and
kicks off ``PowerwallPairingManager``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..const import (
    CONF_POWERWALL_LOCAL_CUSTOMER_PASSWORD,
    CONF_POWERWALL_LOCAL_DIN,
    CONF_POWERWALL_LOCAL_ENERGY_SITE_ID,
    CONF_POWERWALL_LOCAL_IP,
    CONF_POWERWALL_LOCAL_PAIRED,
    CONF_POWERWALL_LOCAL_PAIRED_AT,
    CONF_POWERWALL_LOCAL_PRIVATE_KEY,
    CONF_POWERWALL_LOCAL_PUBLIC_KEY,
    CONF_POWERWALL_LOCAL_VERSION,
    CONF_POWERWALL_LOCAL_WIFI_PASSWORD,
    CONF_POWERWALL_LOCAL_WIFI_SSID,
    CONF_POWERWALL_OFF_GRID_MIN_SOC,
    CONF_POWERWALL_OFFGRID_AS_CURTAILMENT,
    CONF_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS,
    CONF_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC,
    DEFAULT_POWERWALL_OFF_GRID_MIN_SOC,
    DEFAULT_POWERWALL_OFFGRID_AS_CURTAILMENT,
    DEFAULT_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS,
    DEFAULT_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC,
    DOMAIN,
    POWERWALL_PAIRING_WINDOW_SECONDS,
)
from .client import PowerwallLocalClient, PowerwallVersion
from .coordinator import PowerwallLocalCoordinator
from .curtailment_fallback import get_fallback as _get_curtailment_fallback
from .exceptions import PowerwallLocalError, PowerwallPairingError
from .pairing import PowerwallPairingManager
from .signaling import TeslaSignalingClient

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

_RUNTIME_KEY = "powerwall_local"


def _get_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """Return the first PowerSync config entry (we only support one)."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        return entry
    return None


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Get the per-entry runtime dict, creating it if needed."""
    bucket = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    return bucket.setdefault(
        _RUNTIME_KEY,
        {"client": None, "coordinator": None, "pairing_manager": None},
    )


def _get_tesla_fleet_ha_token(hass: HomeAssistant) -> str | None:
    """Try to get a real Tesla token from the tesla_fleet HA integration.

    When the primary PowerSync provider is the proxy (psync_* token),
    the user may also have the tesla_fleet HA integration installed
    which holds a real Tesla JWT token with the scopes needed for
    hermes signaling.
    """
    from homeassistant.const import CONF_ACCESS_TOKEN, CONF_TOKEN

    for entry in hass.config_entries.async_entries("tesla_fleet"):
        try:
            token_data = entry.data.get(CONF_TOKEN, {})
            token = token_data.get(CONF_ACCESS_TOKEN)
            if token and token.startswith("eyJ"):  # JWT format
                _LOGGER.debug(
                    "Found tesla_fleet HA integration token for signaling"
                )
                return token
        except Exception:
            pass
    return None


def _get_fleet_api_context(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[str | None, str | None, int | None]:
    """Look up the Tesla API token, API base URL, and energy site id.

    Returns (token, base_url, site_id) — any element may be None.
    """
    # Lazy import to avoid circular dependencies at module load.
    from .. import get_tesla_api_token
    from ..const import CONF_TESLA_ENERGY_SITE_ID, get_tesla_api_base_url

    token, provider = get_tesla_api_token(hass, entry)
    base = get_tesla_api_base_url(provider)
    site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)
    try:
        site_id = int(site_id) if site_id is not None else None
    except (TypeError, ValueError):
        site_id = None
    return token, base, site_id


async def _build_client(
    hass: HomeAssistant, entry: ConfigEntry
) -> PowerwallLocalClient | None:
    """Construct a PowerwallLocalClient from entry.data after a successful pair."""
    host = entry.data.get(CONF_POWERWALL_LOCAL_IP)
    customer_password = entry.data.get(CONF_POWERWALL_LOCAL_CUSTOMER_PASSWORD, "")
    version_str = entry.data.get(CONF_POWERWALL_LOCAL_VERSION, "pw3")
    private_key_pem = entry.data.get(CONF_POWERWALL_LOCAL_PRIVATE_KEY)
    din = entry.data.get(CONF_POWERWALL_LOCAL_DIN)

    if not host:
        return None

    try:
        version = PowerwallVersion(version_str)
    except ValueError:
        version = PowerwallVersion.PW3

    key_bytes: bytes | None = None
    if isinstance(private_key_pem, str) and private_key_pem:
        key_bytes = private_key_pem.encode()
    elif isinstance(private_key_pem, bytes):
        key_bytes = private_key_pem

    # Fleet API context for the device_command cloud path (off-grid/reconnect).
    fleet_token, fleet_base, fleet_site_id = _get_fleet_api_context(hass, entry)

    # Build the signaling client for PW3 installs with a known DIN.
    # The access token provider returns whatever token is available —
    # psync_ proxy tokens work via the proxy's /hermes_jwt endpoint,
    # real Tesla JWTs work via the Fleet API hermes exchange directly.
    signaling: TeslaSignalingClient | None = None
    if version == PowerwallVersion.PW3 and din:

        async def _access_token_provider() -> str | None:
            token, _base, _site = _get_fleet_api_context(hass, entry)
            if token:
                return token
            # Fall back to tesla_fleet HA integration token
            return _get_tesla_fleet_ha_token(hass)

        signaling = TeslaSignalingClient(
            access_token_provider=_access_token_provider,
            din=din,
        )

    return PowerwallLocalClient(
        host,
        customer_password,
        version=version,
        private_key_pem=key_bytes,
        din=din,
        fleet_api_base=fleet_base,
        fleet_api_token=fleet_token,
        energy_site_id=fleet_site_id,
        signaling=signaling,
    )


async def ensure_coordinator(
    hass: HomeAssistant, entry: ConfigEntry
) -> PowerwallLocalCoordinator | None:
    """Build or return the local monitoring coordinator if the entry is paired.

    Safe to call from ``async_setup_entry`` — returns None if pairing hasn't
    completed yet.
    """
    if not entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
        return None

    runtime = _runtime(hass, entry)
    existing = runtime.get("coordinator")
    if existing is not None:
        return existing

    # Warm up the shared insecure SSL context off the event loop before we
    # construct the client — otherwise transport.__init__ hits
    # ssl.create_default_context() synchronously on the loop and HA logs
    # a blocking-call warning. The context is module-cached so this only
    # pays the cost on first pair / first restart after pair.
    from .transport import get_insecure_ssl_context
    await get_insecure_ssl_context(hass)

    client = await _build_client(hass, entry)
    if client is None:
        return None

    runtime["client"] = client
    coordinator = PowerwallLocalCoordinator(hass, client, entry=entry)
    runtime["coordinator"] = coordinator
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning("Initial Powerwall local refresh failed: %s", err)

    # Start the signaling WebSocket for reliable device_command delivery.
    if client.signaling is not None:
        runtime["signaling"] = client.signaling
        await client.signaling.start()
        _LOGGER.info("Tesla signaling WebSocket started for %s", client.din)

    return coordinator


class PowerwallPairStartView(HomeAssistantView):
    """POST: kick off a pairing attempt."""

    url = "/api/power_sync/powerwall/pair/start"
    name = "api:power_sync:powerwall:pair:start"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        entry = _get_entry(self._hass)
        if entry is None:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"}, status=503
            )

        try:
            payload = await request.json()
        except Exception:
            payload = {}

        gateway_ip = payload.get("gateway_ip") or payload.get("ip")
        customer_password = (
            payload.get("customer_password")
            or payload.get("password")
            or ""
        )
        version_str = (payload.get("version") or "pw3").lower()
        wifi_ssid = payload.get("wifi_ssid") or payload.get("wifi_name")
        wifi_password = payload.get("wifi_password")

        if not gateway_ip:
            return web.json_response(
                {"success": False, "error": "gateway_ip is required"}, status=400
            )

        try:
            version = PowerwallVersion(version_str)
        except ValueError:
            version = PowerwallVersion.PW3

        # Auto-derive customer password from the WiFi password if the user
        # didn't provide one explicitly. The Powerwall REST login at
        # /api/login/Basic accepts the last 5 characters of the gateway WiFi
        # AP password (what pypowerwall calls gw_pwd) as the "customer
        # password". The mobile app already collects this WiFi password for
        # battery health scanning, so for most users the derivation is free
        # — they never have to look up a separate password.
        if not customer_password and wifi_password and len(wifi_password) >= 5:
            customer_password = wifi_password[-5:]
            _LOGGER.info(
                "Derived customer password from WiFi password (last 5 chars)"
            )

        # Mirror app-supplied creds into the entry so HA holds authoritative
        # config independent of the phone that initiated pairing.
        new_data = {
            **entry.data,
            CONF_POWERWALL_LOCAL_IP: gateway_ip,
            CONF_POWERWALL_LOCAL_VERSION: version.value,
            CONF_POWERWALL_LOCAL_CUSTOMER_PASSWORD: customer_password,
        }
        if wifi_ssid:
            new_data[CONF_POWERWALL_LOCAL_WIFI_SSID] = wifi_ssid
        if wifi_password:
            new_data[CONF_POWERWALL_LOCAL_WIFI_PASSWORD] = wifi_password
        self._hass.config_entries.async_update_entry(entry, data=new_data)

        token, base, site_id = _get_fleet_api_context(self._hass, entry)
        if not token or not base:
            return web.json_response(
                {
                    "success": False,
                    "error": "Tesla API not configured — finish PowerSync setup first",
                },
                status=503,
            )

        runtime = _runtime(self._hass, entry)
        old_mgr: PowerwallPairingManager | None = runtime.get("pairing_manager")
        if old_mgr is not None and old_mgr.is_running:
            await old_mgr.cancel()

        session = async_get_clientsession(self._hass)

        async def _on_success(result):
            updated = {
                **entry.data,
                CONF_POWERWALL_LOCAL_PAIRED: True,
                CONF_POWERWALL_LOCAL_PAIRED_AT: time.time(),
                CONF_POWERWALL_LOCAL_PRIVATE_KEY: result.private_key_pem.decode(),
                CONF_POWERWALL_LOCAL_PUBLIC_KEY: result.public_key_der.hex(),
                CONF_POWERWALL_LOCAL_DIN: result.din,
                CONF_POWERWALL_LOCAL_ENERGY_SITE_ID: result.energy_site_id,
            }
            self._hass.config_entries.async_update_entry(entry, data=updated)
            # Spin up the coordinator in the background so local polling begins.
            await ensure_coordinator(self._hass, entry)

        mgr = PowerwallPairingManager(
            session,
            base,
            token,
            energy_site_id=site_id,
            window_seconds=POWERWALL_PAIRING_WINDOW_SECONDS,
            on_success=_on_success,
        )
        runtime["pairing_manager"] = mgr

        try:
            status = await mgr.start()
        except PowerwallPairingError as err:
            return web.json_response(
                {"success": False, "error": str(err)}, status=409
            )

        return web.json_response({"success": True, "status": status.to_dict()})


class PowerwallPairStatusView(HomeAssistantView):
    """GET: poll the current pairing status."""

    url = "/api/power_sync/powerwall/pair/status"
    name = "api:power_sync:powerwall:pair:status"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        entry = _get_entry(self._hass)
        if entry is None:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"}, status=503
            )
        runtime = _runtime(self._hass, entry)
        mgr: PowerwallPairingManager | None = runtime.get("pairing_manager")
        paired = bool(entry.data.get(CONF_POWERWALL_LOCAL_PAIRED))
        if mgr is None:
            return web.json_response(
                {
                    "success": True,
                    "paired": paired,
                    "status": {
                        "state": "verified" if paired else "idle",
                        "message": "",
                        "remaining_seconds": None,
                    },
                }
            )
        return web.json_response(
            {
                "success": True,
                "paired": paired,
                "status": mgr.status().to_dict(),
            }
        )


class PowerwallPairCancelView(HomeAssistantView):
    """POST: cancel an in-flight pairing attempt."""

    url = "/api/power_sync/powerwall/pair/cancel"
    name = "api:power_sync:powerwall:pair:cancel"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        entry = _get_entry(self._hass)
        if entry is None:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"}, status=503
            )
        mgr: PowerwallPairingManager | None = _runtime(self._hass, entry).get(
            "pairing_manager"
        )
        if mgr is None:
            return web.json_response({"success": True, "status": None})
        status = await mgr.cancel()
        return web.json_response({"success": True, "status": status.to_dict()})


class PowerwallPairUnpairView(HomeAssistantView):
    """POST: clear stored key material + local state."""

    url = "/api/power_sync/powerwall/pair/unpair"
    name = "api:power_sync:powerwall:pair:unpair"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        entry = _get_entry(self._hass)
        if entry is None:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"}, status=503
            )
        runtime = _runtime(self._hass, entry)
        mgr: PowerwallPairingManager | None = runtime.get("pairing_manager")
        if mgr is not None and mgr.is_running:
            await mgr.cancel()

        new_data = {**entry.data}
        for key in (
            CONF_POWERWALL_LOCAL_PAIRED,
            CONF_POWERWALL_LOCAL_PRIVATE_KEY,
            CONF_POWERWALL_LOCAL_PUBLIC_KEY,
            CONF_POWERWALL_LOCAL_DIN,
            CONF_POWERWALL_LOCAL_ENERGY_SITE_ID,
            CONF_POWERWALL_LOCAL_PAIRED_AT,
        ):
            new_data.pop(key, None)
        self._hass.config_entries.async_update_entry(entry, data=new_data)

        runtime["client"] = None
        coordinator = runtime.get("coordinator")
        if coordinator is not None:
            coordinator.update_interval = None
        runtime["coordinator"] = None
        runtime["pairing_manager"] = None
        return web.json_response({"success": True})


class PowerwallOffGridView(HomeAssistantView):
    """POST: go off-grid or reconnect to grid."""

    url = "/api/power_sync/powerwall/off_grid"
    name = "api:power_sync:powerwall:off_grid"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        entry = _get_entry(self._hass)
        if entry is None:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"}, status=503
            )
        if not entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
            return web.json_response(
                {"success": False, "error": "Powerwall not paired for local control"},
                status=409,
            )

        try:
            payload = await request.json()
        except Exception:
            payload = {}
        action = str(payload.get("action", "")).lower()
        if action not in ("go_off_grid", "off_grid", "reconnect", "on_grid"):
            return web.json_response(
                {
                    "success": False,
                    "error": "action must be 'go_off_grid' or 'reconnect'",
                },
                status=400,
            )

        coordinator = await ensure_coordinator(self._hass, entry)
        if coordinator is None or coordinator.client is None:
            return web.json_response(
                {"success": False, "error": "Powerwall local client unavailable"},
                status=503,
            )

        if action in ("go_off_grid", "off_grid"):
            min_soc = int(
                entry.data.get(
                    CONF_POWERWALL_OFF_GRID_MIN_SOC,
                    DEFAULT_POWERWALL_OFF_GRID_MIN_SOC,
                )
            )
            snap = coordinator.data
            if snap is not None and snap.soc is not None and snap.soc < min_soc:
                return web.json_response(
                    {
                        "success": False,
                        "error": f"SOC {snap.soc:.0f}% is below safety floor {min_soc}%",
                        "reason": "low_soc",
                    },
                    status=409,
                )
            try:
                ok = await coordinator.client.go_off_grid()
            except PowerwallLocalError as err:
                return web.json_response(
                    {"success": False, "error": str(err)}, status=502
                )
        else:
            try:
                ok = await coordinator.client.reconnect_grid()
            except PowerwallLocalError as err:
                return web.json_response(
                    {"success": False, "error": str(err)}, status=502
                )

        # The off-grid/reconnect command is sent via the cloud and the
        # physical contactor takes a few seconds to change state. Delay
        # the local refresh so the snapshot reflects the new state.
        import asyncio
        await asyncio.sleep(5)
        await coordinator.async_request_refresh()
        return web.json_response(
            {"success": ok, "action": action, "snapshot": coordinator.snapshot_as_api()}
        )


class PowerwallLocalStatusView(HomeAssistantView):
    """GET: live snapshot from the local coordinator."""

    url = "/api/power_sync/powerwall/local_status"
    name = "api:power_sync:powerwall:local_status"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        entry = _get_entry(self._hass)
        if entry is None:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"}, status=503
            )
        paired = bool(entry.data.get(CONF_POWERWALL_LOCAL_PAIRED))
        if not paired:
            return web.json_response({"success": True, "paired": False})
        coordinator = await ensure_coordinator(self._hass, entry)
        if coordinator is None:
            return web.json_response(
                {"success": True, "paired": True, "available": False}
            )
        return web.json_response(
            {
                "success": True,
                "paired": True,
                **coordinator.snapshot_as_api(),
            }
        )


class PowerwallGatewayInfoView(HomeAssistantView):
    """GET gateway metadata derived from Tesla Fleet API ``site_info``.

    Tesla's ``/api/1/energy_sites/{id}/site_info`` response contains a DIN
    in ``id`` formatted ``{part_number}--{serial_number}``. The customer
    password used by the Powerwall's REST login endpoint is the last 5
    characters of ``serial_number``. Surfacing this automatically means
    the pairing wizard can pre-fill the field instead of making the user
    crawl under their gateway to read a sticker.

    Response shape::

        {
            "success": true,
            "gateway_serial": "TG12345678904G",
            "part_number": "STSTSM",
            "suggested_customer_password": "8904G",
            "site_name": "Home",
            "din": "STSTSM--TG12345678904G"
        }

    All fields may be null if site_info is unavailable or the DIN doesn't
    parse — the wizard should fall back to manual entry in that case and
    not treat it as an error.
    """

    url = "/api/power_sync/powerwall/gateway_info"
    name = "api:power_sync:powerwall:gateway_info"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        entry = _get_entry(self._hass)
        if entry is None:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"},
                status=503,
            )

        # Walk hass.data for the running Tesla coordinator so we can read
        # its cached site_info without making a fresh Fleet API call. The
        # coordinator refreshes site_info every 6 hours, so the cache is
        # almost always populated — and when it isn't we fall back to a
        # direct fetch below.
        bucket = self._hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        tesla_coord = bucket.get("tesla_coordinator")
        site_info: dict[str, Any] | None = None
        if tesla_coord is not None:
            cached = getattr(tesla_coord, "_site_info_cache", None)
            if isinstance(cached, dict) and cached:
                site_info = cached

        if site_info is None:
            # Cache miss — fetch directly. We reuse the existing token
            # helper so all three provider paths (PowerSync proxy,
            # Teslemetry, Fleet API) work identically here.
            try:
                from .. import get_tesla_api_token
                from ..const import CONF_TESLA_ENERGY_SITE_ID, get_tesla_api_base_url
                from homeassistant.helpers.aiohttp_client import async_get_clientsession

                token, provider = get_tesla_api_token(self._hass, entry)
                base = get_tesla_api_base_url(provider)
                site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)
                if not token or not base or not site_id:
                    return web.json_response(
                        {
                            "success": False,
                            "error": "Tesla API not configured",
                        },
                        status=503,
                    )
                session = async_get_clientsession(self._hass)
                url = f"{base}/api/1/energy_sites/{site_id}/site_info"
                headers = {"Authorization": f"Bearer {token}"}
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        return web.json_response(
                            {
                                "success": False,
                                "error": f"Fleet API site_info failed ({resp.status})",
                            },
                            status=502,
                        )
                    data = await resp.json()
                    site_info = data.get("response", {}) if isinstance(data, dict) else {}
            except Exception as err:
                _LOGGER.debug("gateway_info fetch error: %s", err)
                return web.json_response(
                    {"success": False, "error": str(err)},
                    status=502,
                )

        din = site_info.get("id") if isinstance(site_info, dict) else None
        site_name = (
            site_info.get("site_name")
            if isinstance(site_info, dict)
            else None
        )

        gateway_serial: str | None = None
        part_number: str | None = None
        if isinstance(din, str) and din:
            parts = din.split("--")
            if len(parts) >= 2:
                # Some DIN formats have >2 parts; the serial is always the
                # last non-empty segment. This matches pypowerwall's parser
                # and handles both ``STSTSM--TG123`` and edge cases like
                # ``TESLA--STSTSM--TG123``.
                tail = [p for p in parts if p]
                if tail:
                    gateway_serial = tail[-1]
                    if len(tail) >= 2:
                        part_number = tail[-2]
            else:
                gateway_serial = din

        suggested_pw = None
        if gateway_serial and len(gateway_serial) >= 5:
            suggested_pw = gateway_serial[-5:]

        return web.json_response(
            {
                "success": True,
                "gateway_serial": gateway_serial,
                "part_number": part_number,
                "suggested_customer_password": suggested_pw,
                "site_name": site_name,
                "din": din,
            }
        )


class PowerwallDiscoverView(HomeAssistantView):
    """GET a list of candidate Powerwall gateway IPs from Home Assistant's mDNS.

    Tesla gateways advertise themselves as ``_teslapowerwall._tcp`` on the
    local network. HA has a built-in zeroconf browser that maintains a live
    cache of service advertisements — we query that cache (no network
    traffic of our own) and return the candidates to the mobile app so the
    pairing wizard can offer a "Detect Gateway" button instead of forcing
    the user to dig through their router's DHCP client list.

    Results are best-effort: an empty list just means the browser hasn't
    seen the gateway advertise yet. Many routers drop mDNS between
    subnets, so a user with a guest / IoT VLAN may need to enter the IP
    manually anyway.
    """

    url = "/api/power_sync/powerwall/discover"
    name = "api:power_sync:powerwall:discover"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        candidates: list[dict[str, Any]] = []
        try:
            # Lazy import so the module still loads on HA installs that
            # somehow have zeroconf disabled.
            from homeassistant.components.zeroconf import async_get_instance

            # HA's async_get_instance() returns an HaZeroconf instance
            # (subclass of zeroconf.Zeroconf) directly — there is no
            # .zeroconf attribute on it. Earlier HA versions wrapped it
            # in an AsyncZeroconf, which is where the old .zeroconf
            # accessor came from; using it on the modern API raises
            # AttributeError.
            zc = await async_get_instance(self._hass)
            # Tesla gateways advertise under several service types across
            # firmware generations — check the common ones. Use the
            # AsyncServiceBrowser cache via async_get_service_info which
            # is the supported path on HaZeroconf.
            service_types = [
                "_teslapowerwall._tcp.local.",
                "_teslanterstudio._tcp.local.",
            ]
            from zeroconf import ServiceBrowser

            for service_type in service_types:
                # entries_with_name is the lowest-level cache read — it
                # returns any DNS record whose name matches. We pull
                # service names out of the cached PTR records then
                # resolve each one into a ServiceInfo with a short
                # timeout to avoid blocking when the record is gone.
                try:
                    cache_entries = list(zc.cache.entries_with_name(service_type))
                except Exception:
                    cache_entries = []
                service_names: set[str] = set()
                for entry in cache_entries:
                    try:
                        alias = getattr(entry, "alias", None)
                        if alias:
                            service_names.add(alias)
                    except Exception:
                        continue

                for name in service_names:
                    try:
                        service_info = zc.get_service_info(
                            service_type, name, timeout=500
                        )
                        if service_info is None:
                            continue
                        addresses: list[str] = []
                        try:
                            addresses = service_info.parsed_addresses() or []
                        except Exception:
                            pass
                        for addr in addresses:
                            candidates.append(
                                {
                                    "ip": addr,
                                    "name": name,
                                    "port": service_info.port,
                                    "service_type": service_type,
                                }
                            )
                    except Exception:
                        # Don't let one bad record break the whole browse.
                        continue
        except Exception as err:
            _LOGGER.debug("Gateway mDNS discover failed: %s", err)

        # Dedupe by IP — multiple service types can advertise the same host.
        seen_ips: set[str] = set()
        deduped = []
        for c in candidates:
            if c["ip"] in seen_ips:
                continue
            seen_ips.add(c["ip"])
            deduped.append(c)

        return web.json_response(
            {"success": True, "candidates": deduped}
        )


class PowerwallSafetyConfigView(HomeAssistantView):
    """GET/POST the manual off-grid SOC floor.

    Separate from ``curtailment_fallback`` because the manual floor applies
    to the always-available ``power_sync.powerwall_go_off_grid`` service
    and the Battery Setup "Go Off-Grid" button — not the opt-in curtailment
    fallback path. Keeping them on different endpoints makes the mental
    model clearer: one knob for "how low can I let the battery get when I
    deliberately go off-grid", one knob for "how low can I let it get when
    PowerSync automatically goes off-grid to block excess export".
    """

    url = "/api/power_sync/powerwall/safety_config"
    name = "api:power_sync:powerwall:safety_config"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        entry = _get_entry(self._hass)
        if entry is None:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"},
                status=503,
            )
        min_soc = int(
            entry.options.get(
                CONF_POWERWALL_OFF_GRID_MIN_SOC,
                entry.data.get(
                    CONF_POWERWALL_OFF_GRID_MIN_SOC,
                    DEFAULT_POWERWALL_OFF_GRID_MIN_SOC,
                ),
            )
        )
        return web.json_response(
            {
                "success": True,
                "off_grid_min_soc": min_soc,
            }
        )

    async def post(self, request: web.Request) -> web.Response:
        entry = _get_entry(self._hass)
        if entry is None:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"},
                status=503,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if "off_grid_min_soc" not in body:
            return web.json_response(
                {"success": False, "error": "off_grid_min_soc required"},
                status=400,
            )
        try:
            v = int(body["off_grid_min_soc"])
        except (TypeError, ValueError):
            return web.json_response(
                {"success": False, "error": "off_grid_min_soc must be an integer"},
                status=400,
            )
        # Hard clamp to the safe range — 0% would let the user fully drain
        # the battery, 90% would make the feature useless.
        clamped = max(5, min(90, v))
        new_options = dict(entry.options)
        new_options[CONF_POWERWALL_OFF_GRID_MIN_SOC] = clamped
        self._hass.config_entries.async_update_entry(entry, options=new_options)
        return web.json_response(
            {"success": True, "off_grid_min_soc": clamped}
        )


class PowerwallCurtailmentFallbackView(HomeAssistantView):
    """GET/POST the Powerwall off-grid curtailment fallback config + status.

    GET returns the current options and the live fallback state so the app
    can show "Currently off-grid due to curtailment" with a session duration
    counter.

    POST updates any of ``enabled``, ``min_soc``, ``max_seconds`` in
    entry.options. The per-entry PowerwallCurtailmentFallback singleton
    re-reads options on every gate check, so there is no need to reset it.
    """

    url = "/api/power_sync/powerwall/curtailment_fallback"
    name = "api:power_sync:powerwall:curtailment_fallback"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        entry = _get_entry(self._hass)
        if entry is None:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"},
                status=503,
            )
        enabled = bool(
            entry.options.get(
                CONF_POWERWALL_OFFGRID_AS_CURTAILMENT,
                entry.data.get(
                    CONF_POWERWALL_OFFGRID_AS_CURTAILMENT,
                    DEFAULT_POWERWALL_OFFGRID_AS_CURTAILMENT,
                ),
            )
        )
        min_soc = int(
            entry.options.get(
                CONF_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC,
                entry.data.get(
                    CONF_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC,
                    DEFAULT_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC,
                ),
            )
        )
        max_seconds = int(
            entry.options.get(
                CONF_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS,
                entry.data.get(
                    CONF_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS,
                    DEFAULT_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS,
                ),
            )
        )
        fallback = _get_curtailment_fallback(self._hass, entry)
        return web.json_response(
            {
                "success": True,
                "config": {
                    "enabled": enabled,
                    "min_soc": min_soc,
                    "max_seconds": max_seconds,
                },
                "status": fallback.status().to_dict(),
            }
        )

    async def post(self, request: web.Request) -> web.Response:
        entry = _get_entry(self._hass)
        if entry is None:
            return web.json_response(
                {"success": False, "error": "PowerSync not configured"},
                status=503,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        new_options = dict(entry.options)
        if "enabled" in body:
            new_options[CONF_POWERWALL_OFFGRID_AS_CURTAILMENT] = bool(body["enabled"])
        if "min_soc" in body:
            try:
                v = int(body["min_soc"])
            except (TypeError, ValueError):
                return web.json_response(
                    {"success": False, "error": "min_soc must be an integer"},
                    status=400,
                )
            new_options[CONF_POWERWALL_OFFGRID_CURTAILMENT_MIN_SOC] = max(
                0, min(100, v)
            )
        if "max_seconds" in body:
            try:
                v = int(body["max_seconds"])
            except (TypeError, ValueError):
                return web.json_response(
                    {"success": False, "error": "max_seconds must be an integer"},
                    status=400,
                )
            # Clamp 10 minutes … 24 hours.
            new_options[CONF_POWERWALL_OFFGRID_CURTAILMENT_MAX_SECONDS] = max(
                600, min(86400, v)
            )
        self._hass.config_entries.async_update_entry(entry, options=new_options)
        return await self.get(request)


def register_views(hass: HomeAssistant) -> None:
    """Wire up every Powerwall-local view onto the HA http app."""
    hass.http.register_view(PowerwallPairStartView(hass))
    hass.http.register_view(PowerwallPairStatusView(hass))
    hass.http.register_view(PowerwallPairCancelView(hass))
    hass.http.register_view(PowerwallPairUnpairView(hass))
    hass.http.register_view(PowerwallOffGridView(hass))
    hass.http.register_view(PowerwallLocalStatusView(hass))
    hass.http.register_view(PowerwallSafetyConfigView(hass))
    hass.http.register_view(PowerwallCurtailmentFallbackView(hass))
    hass.http.register_view(PowerwallDiscoverView(hass))
    hass.http.register_view(PowerwallGatewayInfoView(hass))
    hass.http.register_view(PowerwallDebugConfigView(hass))


class PowerwallDebugConfigView(HomeAssistantView):
    """TEMPORARY: dump gateway config.json for debugging islanding keys."""
    url = "/api/power_sync/powerwall/debug_config"
    name = "api:power_sync:powerwall:debug_config"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        entry = _get_entry(self._hass)
        if entry is None:
            return web.json_response({"error": "no entry"}, status=503)
        coordinator = await ensure_coordinator(self._hass, entry)
        if coordinator is None or coordinator.client is None:
            return web.json_response({"error": "no client"}, status=503)
        client = coordinator.client
        # Login + fetch DIN
        await client.login()
        din = client.din
        if not din or not client._transport:
            return web.json_response({"error": f"no din={din}"}, status=503)
        config = await client._transport.read_config(din)
        if config is None:
            return web.json_response({"error": "read_config returned None"}, status=502)
        return web.json_response({"success": True, "config": config})
