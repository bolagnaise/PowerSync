<!-- release: v2.12.897 -->

## What's Changed

**Flow Power live and planned prices now use the same PEA formula**
The current-price tariff path once again subtracts the average daily network tariff used by Flow Power's v2 PEA calculation, matching the optimizer. This fixes current import prices that could display several cents per kWh above the corresponding optimized price.

**Existing Flow Power pricing settings stay consistent everywhere**
Legacy configurations that still store the base rate, custom PEA, or PEA-enabled setting in the original config entry now apply those values to tariff display, optimization, and energy cost tracking instead of allowing one path to fall back to defaults.

Update available via HACS
