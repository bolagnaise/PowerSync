"""Inverter controller module for direct solar curtailment.

Provides a factory function to get the appropriate inverter controller
based on the configured brand.
"""
import logging
from typing import Optional

from .base import InverterController

_LOGGER = logging.getLogger(__name__)

# Supported inverter brands for curtailment control
INVERTER_BRANDS = {
    "sungrow": "Sungrow",
    "fronius": "Fronius",
    "goodwe": "GoodWe",
    "huawei": "Huawei",
    "enphase": "Enphase",
    "zeversolar": "Zeversolar",
    "sigenergy": "Sigenergy",
}

# Fronius models (SunSpec Modbus)
# Requires installer password for 0W export limit configuration
FRONIUS_MODELS = {
    "primo": "Primo (Single Phase)",
    "symo": "Symo (Three Phase)",
    "gen24": "Gen24 / Tauro",
    "eco": "Eco",
}

# GoodWe models (ET/EH/BT/BH series support export limiting)
# Note: DT/D-NS series do NOT support export limiting via Modbus
GOODWE_MODELS = {
    "et": "ET Series (Hybrid)",
    "eh": "EH Series (Hybrid)",
    "bt": "BT Series (Hybrid)",
    "bh": "BH Series (Hybrid)",
    "es": "ES Series (Hybrid)",
    "em": "EM Series (Hybrid)",
}

# Huawei SUN2000 series (via Smart Dongle Modbus TCP)
# Reference: https://github.com/wlcrs/huawei-solar-lib
# L1 Series (Single Phase Hybrid)
HUAWEI_L1_MODELS = {
    "sun2000-2ktl-l1": "SUN2000-2KTL-L1",
    "sun2000-3ktl-l1": "SUN2000-3KTL-L1",
    "sun2000-3.68ktl-l1": "SUN2000-3.68KTL-L1",
    "sun2000-4ktl-l1": "SUN2000-4KTL-L1",
    "sun2000-4.6ktl-l1": "SUN2000-4.6KTL-L1",
    "sun2000-5ktl-l1": "SUN2000-5KTL-L1",
    "sun2000-6ktl-l1": "SUN2000-6KTL-L1",
}

# M0/M1 Series (Three Phase)
HUAWEI_M1_MODELS = {
    "sun2000-3ktl-m0": "SUN2000-3KTL-M0",
    "sun2000-4ktl-m0": "SUN2000-4KTL-M0",
    "sun2000-5ktl-m0": "SUN2000-5KTL-M0",
    "sun2000-6ktl-m0": "SUN2000-6KTL-M0",
    "sun2000-8ktl-m0": "SUN2000-8KTL-M0",
    "sun2000-10ktl-m0": "SUN2000-10KTL-M0",
    "sun2000-3ktl-m1": "SUN2000-3KTL-M1",
    "sun2000-4ktl-m1": "SUN2000-4KTL-M1",
    "sun2000-5ktl-m1": "SUN2000-5KTL-M1",
    "sun2000-6ktl-m1": "SUN2000-6KTL-M1",
    "sun2000-8ktl-m1": "SUN2000-8KTL-M1",
    "sun2000-10ktl-m1": "SUN2000-10KTL-M1",
}

# M2 Series (Three Phase, Higher Power)
HUAWEI_M2_MODELS = {
    "sun2000-8ktl-m2": "SUN2000-8KTL-M2",
    "sun2000-10ktl-m2": "SUN2000-10KTL-M2",
    "sun2000-12ktl-m2": "SUN2000-12KTL-M2",
    "sun2000-15ktl-m2": "SUN2000-15KTL-M2",
    "sun2000-17ktl-m2": "SUN2000-17KTL-M2",
    "sun2000-20ktl-m2": "SUN2000-20KTL-M2",
}

# Combined Huawei models
HUAWEI_MODELS = {
    **HUAWEI_L1_MODELS,
    **HUAWEI_M1_MODELS,
    **HUAWEI_M2_MODELS,
}

# Enphase microinverter systems (via IQ Gateway/Envoy REST API)
# Reference: https://github.com/pyenphase/pyenphase
# Note: Requires JWT token for firmware 7.x+, DPEL requires installer access
ENPHASE_GATEWAY_MODELS = {
    "envoy": "Envoy (Legacy)",
    "envoy-s": "Envoy-S",
    "envoy-s-metered": "Envoy-S Metered",
    "iq-gateway": "IQ Gateway",
    "iq-gateway-metered": "IQ Gateway Metered",
}

ENPHASE_MICROINVERTER_MODELS = {
    "iq7": "IQ7 Series",
    "iq7+": "IQ7+ Series",
    "iq7a": "IQ7A Series",
    "iq7x": "IQ7X Series",
    "iq8": "IQ8 Series",
    "iq8+": "IQ8+ Series",
    "iq8a": "IQ8A Series",
    "iq8m": "IQ8M Series",
    "iq8h": "IQ8H Series",
}

# Combined Enphase models (show gateway models in dropdown)
ENPHASE_MODELS = {
    **ENPHASE_GATEWAY_MODELS,
}

# Zeversolar models (via HTTP API to built-in web interface)
# Uses POST to /pwrlim.cgi for power limiting
ZEVERSOLAR_MODELS = {
    "tlc5000": "TLC5000",
    "tlc6000": "TLC6000",
    "tlc8000": "TLC8000",
    "tlc10000": "TLC10000",
    "zeversolair-mini-3000": "Zeversolair Mini 3000",
    "zeversolair-tl3000": "Zeversolair TL3000",
}

# Sungrow SG series (string inverters) - single phase residential
SUNGROW_SG_MODELS = {
    "sg2.5rs": "SG2.5RS",
    "sg3.0rs": "SG3.0RS",
    "sg3.6rs": "SG3.6RS",
    "sg4.0rs": "SG4.0RS",
    "sg5.0rs": "SG5.0RS",
    "sg6.0rs": "SG6.0RS",
    "sg7.0rs": "SG7.0RS",
    "sg8.0rs": "SG8.0RS",
    "sg10rs": "SG10RS",
    "sg12rs": "SG12RS",
    "sg15rs": "SG15RS",
    "sg17rs": "SG17RS",
    "sg20rs": "SG20RS",
}

# Sungrow SH series (hybrid inverters with battery)
# Reference: https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant
# Single phase RS series
SUNGROW_SH_RS_MODELS = {
    "sh3.0rs": "SH3.0RS",
    "sh3.6rs": "SH3.6RS",
    "sh4.0rs": "SH4.0RS",
    "sh4.6rs": "SH4.6RS",
    "sh5.0rs": "SH5.0RS",
    "sh6.0rs": "SH6.0RS",
}

# Three phase RT series (residential)
SUNGROW_SH_RT_MODELS = {
    "sh5.0rt": "SH5.0RT",
    "sh6.0rt": "SH6.0RT",
    "sh8.0rt": "SH8.0RT",
    "sh10rt": "SH10RT",
    "sh5.0rt-20": "SH5.0RT-20",
    "sh6.0rt-20": "SH6.0RT-20",
    "sh8.0rt-20": "SH8.0RT-20",
    "sh10rt-20": "SH10RT-20",
    "sh8.0rt-v112": "SH8.0RT-V112",
    "sh10rt-v112": "SH10RT-V112",
}

# Three phase T series (commercial/C&I)
SUNGROW_SH_T_MODELS = {
    "sh15t": "SH15T",
    "sh20t": "SH20T",
    "sh25t": "SH25T",
}

# Legacy SH models
SUNGROW_SH_LEGACY_MODELS = {
    "sh3k6": "SH3K6",
    "sh4k6": "SH4K6",
    "sh5k-20": "SH5K-20",
    "sh5k-30": "SH5K-30",
    "sh5k-v13": "SH5K-V13",
}

# Combined SH models
SUNGROW_SH_MODELS = {
    **SUNGROW_SH_RS_MODELS,
    **SUNGROW_SH_RT_MODELS,
    **SUNGROW_SH_T_MODELS,
    **SUNGROW_SH_LEGACY_MODELS,
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
    token: Optional[str] = None,
    load_following: bool = False,
) -> Optional[InverterController]:
    """Factory function to get the appropriate inverter controller.

    Args:
        brand: Inverter brand (e.g., 'sungrow')
        host: IP address of the inverter/gateway
        port: Modbus TCP port (default: 502)
        slave_id: Modbus slave ID (default: 1)
        model: Inverter model (optional, for brand-specific features)
        token: JWT token for Enphase IQ Gateway authentication (firmware 7.x+)
        load_following: Fronius-specific - use calculated power limits instead
                       of relying on 0W soft export limit (default: False)

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

    if brand_lower == "fronius":
        from .fronius import FroniusController
        return FroniusController(
            host=host,
            port=port,
            slave_id=slave_id,
            model=model,
            load_following=load_following,
        )

    if brand_lower == "goodwe":
        from .goodwe import GoodWeController
        # GoodWe default slave ID is 247
        if slave_id == 1:
            slave_id = 247
        return GoodWeController(
            host=host,
            port=port,
            slave_id=slave_id,
            model=model,
        )

    if brand_lower == "huawei":
        from .huawei import HuaweiController
        return HuaweiController(
            host=host,
            port=port,
            slave_id=slave_id,
            model=model,
        )

    if brand_lower == "enphase":
        from .enphase import EnphaseController
        # Enphase uses HTTPS on port 443, not Modbus
        if port == 502:
            port = 443
        return EnphaseController(
            host=host,
            port=port,
            slave_id=slave_id,
            model=model,
            token=token,
        )

    if brand_lower == "zeversolar":
        from .zeversolar import ZeversolarController
        # Zeversolar uses HTTP on port 80, not Modbus
        if port == 502:
            port = 80
        return ZeversolarController(
            host=host,
            port=port,
            slave_id=slave_id,
            model=model,
        )

    if brand_lower == "sigenergy":
        from .sigenergy import SigenergyController
        return SigenergyController(
            host=host,
            port=port,
            slave_id=slave_id,
            model=model,
        )

    _LOGGER.error(f"Unsupported inverter brand: {brand}")
    return None
