<!-- release: v2.12.850 -->

## What's Changed

**SolaX reserve bounds are applied independently**

Hardened the SolaX minimum-SOC fix for installations that expose separate self-use and grid-tied reserve controls. Each Home Assistant number entity now uses its own advertised range, preserving 10% support on newer models without sending an out-of-range value to a legacy control whose minimum remains 15%.

Update available via HACS
