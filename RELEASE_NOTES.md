<!-- release: v2.12.1634 -->

## Amber recovery after restart

For Amber sites managed by PowerSync with automatic tariff sync enabled, unfinished charge and discharge cleanup now uses current Amber prices. A different old PowerSync force tariff no longer leaves recovery stuck waiting for its original tariff.

Update through HACS and restart Home Assistant. Saved recovery files are reused automatically. Recovery still waits if fresh prices, Tesla confirmation or hardware restoration are unavailable.
