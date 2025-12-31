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

# Sungrow SG series (string inverters)
SUNGROW_SG_MODELS = {
    "sg5": "SG5.0RS",
    "sg8": "SG8.0RS",
    "sg10": "SG10RS",
    "sg12": "SG12RS",
    "sg15": "SG15RS",
    "sg20": "SG20RS",
}

# Sungrow SH series (hybrid inverters with battery)
# Reference: https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant
SUNGROW_SH_MODELS = {
    "sh3.6rs": "SH3.6RS",
    "sh4.6rs": "SH4.6RS",
    "sh5.0rs": "SH5.0RS",
    "sh5.0rt": "SH5.0RT",
    "sh6.0rs": "SH6.0RS",
    "sh6.0rt": "SH6.0RT",
    "sh8.0rt": "SH8.0RT",
    "sh10rt": "SH10RT",
    "sh5k20": "SH5K-20",
    "sh5k30": "SH5K-30",
}

# Combined model list for UI dropdowns
SUNGROW_MODELS = {
    **SUNGROW_SG_MODELS,
    **SUNGROW_SH_MODELS,
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
        # Determine which controller based on model prefix
        # SH series (hybrid) uses different registers than SG series (string)
        model_lower = model.lower() if model else ""
        if model_lower.startswith("sh"):
            from .sungrow_sh import SungrowSHController
            return SungrowSHController(
                host=host,
                port=port,
                slave_id=slave_id,
                model=model,
            )
        else:
            # Default to SG series controller
            from .sungrow import SungrowController
            return SungrowController(
                host=host,
                port=port,
                slave_id=slave_id,
                model=model,
            )

    _LOGGER.error(f"Unsupported inverter brand: {brand}")
    return None
