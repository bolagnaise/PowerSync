"""Data update coordinator for GloBird HA."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Awaitable, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .globird_api import (
    GloBirdClient,
    build_cost_summary,
    build_latest_data_status,
    build_usage_summary,
    build_weather_summary,
    extract_accounts_and_services,
    select_meter_for_service,
    service_id,
)
from .const import (
    CONF_GLOBIRD_EMAIL,
    CONF_GLOBIRD_PASSWORD,
    DOMAIN,
    GLOBIRD_ACCOUNT_UPDATE_INTERVAL_SECONDS,
    GLOBIRD_DEFAULT_USAGE_DAYS,
    GLOBIRD_STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class GloBirdCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for fetching GloBird portal data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_globird",
            update_interval=timedelta(seconds=GLOBIRD_ACCOUNT_UPDATE_INTERVAL_SECONDS),
        )

        self.entry = entry
        self.email = entry.options.get(
            CONF_GLOBIRD_EMAIL, entry.data.get(CONF_GLOBIRD_EMAIL, "")
        )
        self.password = entry.options.get(
            CONF_GLOBIRD_PASSWORD, entry.data.get(CONF_GLOBIRD_PASSWORD, "")
        )
        self.client = GloBirdClient()

        self._cache_store = Store(
            hass, GLOBIRD_STORAGE_VERSION, f"{DOMAIN}.globird.cache.{entry.entry_id}"
        )
        self._cookie_store = Store(
            hass, GLOBIRD_STORAGE_VERSION, f"{DOMAIN}.globird.cookies.{entry.entry_id}"
        )
        self._cache: dict[str, Any] | None = None
        self._initialized = False

    async def async_shutdown(self) -> None:
        """Close resources."""
        await self.client.close()

    async def _async_initialize(self) -> None:
        """Load cached data and any persisted cookies."""
        if self._initialized:
            return

        loaded_cache = await self._cache_store.async_load()
        self._cache = loaded_cache if isinstance(loaded_cache, dict) else None
        cookie_state = await self._cookie_store.async_load()
        cookies = cookie_state.get("cookies", []) if isinstance(cookie_state, dict) else []
        if isinstance(cookies, list) and cookies:
            self.client.import_session_cookies(cookies)
            restored = await self.client.restore_session(self.email, self.password)
            if restored is not None:
                _LOGGER.info("GloBird session restored from persisted cookies")

        self._initialized = True

    async def _fetch_optional(
        self,
        key: str,
        callback: Callable[[], Awaitable[dict[str, Any]]],
        cache: dict[str, Any],
        *,
        _errors: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Fetch optional data, falling back to cache on endpoint failure."""
        try:
            return await callback()
        except Exception as err:  # noqa: BLE001 - optional portal endpoint.
            _LOGGER.warning("GloBird optional fetch failed for %s: %s", key, err)
            if _errors is not None:
                _errors[key] = str(err)
            cached_value = cache.get(key)
            return cached_value if isinstance(cached_value, dict) else None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch GloBird data."""
        await self._async_initialize()
        cache = self._cache or {}

        try:
            if self.client.is_authenticated:
                # Session cookies are still valid — _request_json will automatically
                # re-authenticate (fresh_session=False) if a 403 is returned.
                current_user = await self.client.get_current_user()
            else:
                current_user = await self.client.authenticate(self.email, self.password)

            accounts, services = extract_accounts_and_services(current_user)

            # Extract primary identifiers for account-scoped endpoints
            primary_account_id = (
                services[0].get("accountId") if services
                else (accounts[0].get("accountId") if accounts else None)
            )
            primary_nmi = services[0].get("siteIdentifier") if services else None
            primary_account_service_id = services[0].get("accountServiceId") if services else None

            fetch_errors: dict[str, str] = {}
            self.client.disable_reauth()
            try:
                data: dict[str, Any] = {
                    "current_user": current_user,
                    "accounts": accounts,
                    "services": services,
                    "last_update": time.time(),
                }

                data["dashboard"] = await self._fetch_optional(
                    "dashboard",
                    lambda: self.client.get_dashboard(account_id=primary_account_id),
                    cache,
                    _errors=fetch_errors,
                )
                data["balance"] = await self._fetch_optional(
                    "balance",
                    lambda: self.client.get_balance(account_id=primary_account_id),
                    cache,
                    _errors=fetch_errors,
                )
                data["signup_info"] = await self._fetch_optional(
                    "signup_info",
                    lambda: self.client.get_signup_info(account_id=primary_account_id),
                    cache,
                    _errors=fetch_errors,
                )
                data["service_status"] = await self._fetch_optional(
                    "service_status", self.client.get_account_service_status, cache, _errors=fetch_errors
                )
                data["meter_types"] = await self._fetch_optional(
                    "meter_types",
                    lambda: self.client.get_power_meter_types(nmi=primary_nmi),
                    cache,
                    _errors=fetch_errors,
                )
                data["read_meters"] = await self._fetch_optional(
                    "read_meters",
                    lambda: self.client.get_read_meters(account_service_id=primary_account_service_id),
                    cache,
                    _errors=fetch_errors,
                )
                data["weather_impacted_days"] = await self._fetch_optional(
                    "weather_impacted_days",
                    lambda: self.client.get_weather_impacted_days(account_id=primary_account_id),
                    cache,
                    _errors=fetch_errors,
                )
                data["_fetch_errors"] = fetch_errors
            finally:
                self.client.enable_reauth()

            cached_service_data = cache.get("service_data", {})
            cached_service_data = (
                cached_service_data if isinstance(cached_service_data, dict) else {}
            )
            service_data = {}
            for service in services:
                sid = service_id(service)
                cached_detail = cached_service_data.get(sid)
                service_data[sid] = await self._fetch_service_detail(
                    service,
                    data.get("read_meters"),
                    data.get("service_status"),
                    cached_detail if isinstance(cached_detail, dict) else {},
                )

            data["service_data"] = service_data

            self._cache = data
            await self._cache_store.async_save(data)
            await self._cookie_store.async_save({
                "cookies": self.client.export_session_cookies(),
            })
            return data

        except Exception as err:  # noqa: BLE001 - coordinator should preserve cache.
            if cache:
                stale = dict(cache)
                stale["refresh_error"] = str(err)
                stale["last_failed_update"] = time.time()
                return stale
            raise UpdateFailed(f"Unable to fetch GloBird data: {err}") from err

    async def _fetch_service_detail(
        self,
        service: dict[str, Any],
        meters_payload: dict[str, Any] | None,
        status_payload: dict[str, Any] | None,
        cache: dict[str, Any],
    ) -> dict[str, Any]:
        """Fetch heavier per-service detail."""
        sid = service_id(service)
        status_map = (
            status_payload.get("data", {})
            if isinstance(status_payload, dict)
            else {}
        )
        service_status = status_map.get(sid) if isinstance(status_map, dict) else None

        meter = select_meter_for_service(service, meters_payload)
        identifier = service.get("siteIdentifier")
        serial_number = meter.get("serialNumber") if meter else None
        meter_read_type = str(meter.get("meterReadType") or "" if meter else "")
        is_smart = meter_read_type.lower() != "basic"
        account_service_id = service.get("accountServiceId")

        usage = None
        if identifier and serial_number:
            usage = await self._fetch_optional(
                "usage",
                lambda: self.client.get_usage(
                    identifier=str(identifier),
                    serial_number=str(serial_number),
                    account_service_id=account_service_id,
                    is_smart=is_smart,
                    days=GLOBIRD_DEFAULT_USAGE_DAYS,
                ),
                cache,
            )

        cost = None
        if identifier and account_service_id:
            cost = await self._fetch_optional(
                "cost",
                lambda: self.client.get_cost_detail(
                    account_service_id=account_service_id,
                    identifier=str(identifier),
                    is_smart=is_smart,
                    days=GLOBIRD_DEFAULT_USAGE_DAYS,
                ),
                cache,
            )

        weather = None
        post_code = service.get("postCode")
        if post_code and account_service_id:
            weather = await self._fetch_optional(
                "weather",
                lambda: self.client.get_weather_data(
                    account_service_id=account_service_id,
                    post_code=str(post_code),
                    days=GLOBIRD_DEFAULT_USAGE_DAYS,
                ),
                cache,
            )

        usage_summary = build_usage_summary(usage)
        cost_summary = build_cost_summary(cost)

        return {
            "service": service,
            "status": service_status,
            "meter": meter,
            "usage": usage,
            "usage_summary": usage_summary,
            "cost": cost,
            "cost_summary": cost_summary,
            "latest_data_status": build_latest_data_status(
                usage_summary, cost_summary
            ),
            "weather": weather,
            "weather_summary": build_weather_summary(weather),
        }
