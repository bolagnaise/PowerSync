<!-- release: v2.12.403 -->

## What's Changed

**Fix Enphase inverter status session cleanup**
PowerSync now always closes the Enphase inverter controller after mobile inverter-status checks, even when the status request errors or is interrupted during Gateway or Enlighten token work. This prevents Home Assistant from logging repeated `Unclosed client session` and `Unclosed connector` errors after enabling Enphase AC curtailment.

Update available via HACS
