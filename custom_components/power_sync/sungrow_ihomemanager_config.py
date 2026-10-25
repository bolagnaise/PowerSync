"""Validation shared by native iHomeManager setup and runtime."""
from __future__ import annotations

from .const import (
    CONF_SUNGROW_HOST, CONF_SUNGROW_PORT, CONF_SUNGROW_SLAVE_ID,
    CONF_SUNGROW_IHOMEMANAGER_HOST, CONF_SUNGROW_IHOMEMANAGER_PORT,
)


def native_connection_settings(values):
    """Require distinct, explicit telemetry and native control endpoints."""
    def integer(value):
        try:
            numeric = float(value)
        except (TypeError, ValueError) as err:
            raise ValueError("Invalid Modbus endpoint number") from err
        if isinstance(value, bool) or not numeric.is_integer():
            raise ValueError("Modbus ports and units must be integers")
        return int(numeric)

    native_host = str(values.get(CONF_SUNGROW_IHOMEMANAGER_HOST) or "").strip()
    native_port = integer(values.get(CONF_SUNGROW_IHOMEMANAGER_PORT, 503))
    inverter_host = str(values.get(CONF_SUNGROW_HOST) or "").strip()
    inverter_port = integer(values.get(CONF_SUNGROW_PORT, 502))
    inverter_unit = integer(values.get(CONF_SUNGROW_SLAVE_ID, 1))
    if not native_host or not inverter_host:
        raise ValueError("Both inverter telemetry and iHomeManager control hosts are required")
    if native_port not in (503, 504) or not 1 <= inverter_port <= 65535 or not 1 <= inverter_unit <= 246:
        raise ValueError("Use native plaintext port 503/504 and the inverter's telemetry unit")
    if native_host.casefold() == inverter_host.casefold() and native_port == inverter_port:
        raise ValueError("Inverter telemetry and native iHomeManager control need separate TCP endpoints")
    return {CONF_SUNGROW_IHOMEMANAGER_HOST: native_host, CONF_SUNGROW_IHOMEMANAGER_PORT: native_port}
