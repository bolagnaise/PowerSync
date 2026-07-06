<!-- release: v2.12.772 -->

## What's Changed

**Sungrow WiNet-S zero-export protection**
Sungrow SH-series export limiting now avoids writing an enabled `0W` export limit to the WiNet-S export-limit register. Zero-export curtailment requests are applied with a `50W` safety floor instead, which prevents WiNet-S Modbus lockups while keeping the inverter effectively curtailed.

**Sigenergy tariff sync reload guard**
Sigenergy tariff sync now recreates the PowerSync entry cache before saving tariff upload state. This prevents reload/unload timing from raising a `KeyError` after a tariff upload completes while Home Assistant is reloading the integration.

**Flow Power PEA v2 pricing correction**
Flow Power v2 import pricing now applies the formula as `GST*Spot + Tariff - GST*TWAP - BPEA`, matching the corrected PEA calculation used by the pricing tests.

Update available via HACS
