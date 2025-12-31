"""Inverter controller module for direct solar curtailment.

Provides a factory function to get the appropriate inverter controller
based on the configured brand.
"""
import logging
from typing import Optional

from .base import InverterController

_LOGGER = logging.getLogger(__name__)

# Supported inverter brands
INVERTER_BRANDS = {
    "sungrow": "Sungrow",
}

# Sungrow model options
SUNGROW_MODELS = {
    "sg5": "SG5.0RS",
    "sg8": "SG8.0RS",
    "sg10": "SG10RS",
    "sg12": "SG12RS",
    "sg15": "SG15RS",
    "sg20": "SG20RS",
}


def get_inverter_controller(
    brand: str,
    host: str,
    port: int = 502,
    slave_id: int = 1,
    model: Optional[str] = None,
) -> Optional[InverterController]:
    """Factory function to get the appropriate inverter controller.

    Args:
        brand: Inverter brand (e.g., 'sungrow')
        host: IP address of the inverter/gateway
        port: Modbus TCP port (default: 502)
        slave_id: Modbus slave ID (default: 1)
        model: Inverter model (optional, for brand-specific features)

    Returns:
        InverterController instance or None if brand not supported
    """
    brand_lower = brand.lower() if brand else ""

    if brand_lower == "sungrow":
        from .sungrow import SungrowController
        return SungrowController(
            host=host,
            port=port,
            slave_id=slave_id,
            model=model,
        )

    _LOGGER.error(f"Unsupported inverter brand: {brand}")
    return None
