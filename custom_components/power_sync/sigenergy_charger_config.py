"""Helpers for resolving Sigenergy EV charger connection settings."""

from __future__ import annotations

from typing import Any, Mapping

from .const import (
    CONF_SIGENERGY_CHARGER_HOST,
    CONF_SIGENERGY_CHARGER_PORT,
    CONF_SIGENERGY_CHARGER_SLAVE_ID,
    CONF_SIGENERGY_CHARGER_TYPE,
    CONF_SIGENERGY_MODBUS_HOST,
    DEFAULT_SIGENERGY_CHARGER_PORT,
    DEFAULT_SIGENERGY_CHARGER_SLAVE_ID,
    DOMAIN,
    SIGENERGY_CHARGER_EVAC,
)


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _present(value: Any) -> bool:
    return value not in (None, "")


def _stored_sigenergy_charger_config(
    hass: Any | None,
    entry_id: str | None,
) -> Mapping[str, Any] | None:
    if not hass or not entry_id:
        return None

    entry_data = getattr(hass, "data", {}).get(DOMAIN, {}).get(entry_id, {})
    store = entry_data.get("automation_store") if isinstance(entry_data, Mapping) else None
    stored_data = getattr(store, "_data", {}) or {}
    for config in stored_data.get("vehicle_charging_configs", []):
        if not isinstance(config, Mapping):
            continue
        if (
            config.get("vehicle_id") == "sigenergy_charger"
            or config.get("charger_type") == "sigenergy"
            or _present(config.get("sigenergy_charger_host"))
        ):
            return config
    return None


def resolve_sigenergy_charger_connection(
    entry: Any | None,
    *,
    hass: Any | None = None,
    fallback_host: str | None = None,
) -> dict[str, Any]:
    """Return the effective Sigenergy EV charger Modbus connection details."""
    opts = {
        **getattr(entry, "data", {}),
        **getattr(entry, "options", {}),
    } if entry else {}
    entry_id = getattr(entry, "entry_id", None)
    stored = _stored_sigenergy_charger_config(hass, entry_id)

    entry_host = _clean_string(opts.get(CONF_SIGENERGY_CHARGER_HOST))
    modbus_host = _clean_string(opts.get(CONF_SIGENERGY_MODBUS_HOST))
    stored_host = _clean_string(
        stored.get("sigenergy_charger_host") if stored else None
    )
    entry_host_is_dedicated = bool(entry_host and entry_host != modbus_host)
    prefer_stored = bool(stored and not entry_host_is_dedicated)

    if entry_host_is_dedicated:
        host = entry_host
    elif stored_host:
        host = stored_host
    else:
        host = entry_host or modbus_host or _clean_string(fallback_host)

    def choose(stored_key: str, option_key: str, default: Any) -> Any:
        if prefer_stored and stored and _present(stored.get(stored_key)):
            return stored.get(stored_key)
        if _present(opts.get(option_key)):
            return opts.get(option_key)
        if stored and _present(stored.get(stored_key)):
            return stored.get(stored_key)
        return default

    return {
        "host": host,
        "port": choose(
            "sigenergy_charger_port",
            CONF_SIGENERGY_CHARGER_PORT,
            DEFAULT_SIGENERGY_CHARGER_PORT,
        ),
        "slave_id": choose(
            "sigenergy_charger_slave_id",
            CONF_SIGENERGY_CHARGER_SLAVE_ID,
            DEFAULT_SIGENERGY_CHARGER_SLAVE_ID,
        ),
        "charger_type": choose(
            "sigenergy_charger_type",
            CONF_SIGENERGY_CHARGER_TYPE,
            SIGENERGY_CHARGER_EVAC,
        ),
    }
