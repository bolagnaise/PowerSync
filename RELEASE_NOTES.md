<!-- release: v2.12.533 -->

## What's Changed

**Powerwall local reserve auto-calibration**
PowerSync now detects the hidden reserve offset used by paired local Powerwall gateways by comparing the local gateway readback with Tesla cloud site info. This keeps reserve controls aligned for installs where the gateway low-SOE reserve differs from the default 5%.

**Flow Power tariff sensor refreshes**
Flow Power wholesale, network tariff, and Amber comparison sensors now refresh their Home Assistant state as soon as PowerSync recomputes tariff data, so tariff-derived values update without waiting for a broader coordinator refresh.

Update available via HACS
