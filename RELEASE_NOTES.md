<!-- release: v2.12.1548 -->
### Steady Tesla BLE charging readings

PowerSync can now keep EV and Home power readings available during steady charging when the BLE bridge supplies raw receipt timestamps. Enable the bridge's charging-freshness package and update its firmware to use this support; older bridges retain the existing 90-second reading expiry.
