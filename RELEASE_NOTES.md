<!-- release: v2.12.842 -->

## What's Changed

Sigenergy charge and discharge rate limits set in Controls now persist across restart and reload, constrain Smart Optimization and force targets without raising stricter optimizer settings, and restore after temporary optimizer actions. Returning control to native or VPP operation still restores the inverter-rated limits, while incomplete hardware restores remain retryable instead of being reported as successful.

Update available via HACS.
