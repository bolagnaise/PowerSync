## What's Changed

**GoodWe: Battery Reserve Now Respected Below 11%**
A hard cap in the GoodWe battery controller was silently clamping the on-grid depth of discharge to 89% (minimum SOC = 11%), regardless of the reserve configured in PowerSync. If your backup reserve was set to 5%, the hardware was still using 11% as the floor — causing the battery to appear stuck and the system to import from grid even with usable capacity remaining. The cap has been removed; DOD now goes up to 100%, so the hardware honours your actual configured reserve.

Update available via HACS
