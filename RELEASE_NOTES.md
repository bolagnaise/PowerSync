<!-- release: v2.12.787 -->

## What's Changed

**Sungrow spread export respects the configured discharge ceiling**
PowerSync now caps the optional Sungrow discharge-headroom write during spread export to the configured maximum discharge power before sending it to the inverter. This prevents SH20T-style systems from repeatedly attempting an above-inverter-limit `33047` write such as `23.8 kW` when the plan itself only asked for a low site export target. If the optional discharge-limit register is already known to be unavailable, spread export now continues with the grid export-limit command instead of re-probing the same rejected register on every refresh.

**Tesla local Powerwall readback appears sooner in controls**
PowerSync now prefers fresh local Powerwall readback for reserve and mode values when available, so locally-written Powerwall settings can appear in the Home Assistant controls more quickly instead of waiting for the next Tesla cloud sync.

Update available via HACS
