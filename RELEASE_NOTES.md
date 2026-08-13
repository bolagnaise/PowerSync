<!-- release: v2.12.1088 -->

## What's Changed

**Keep SolaX manual exports supplying the house**

SolaX manual-mode control now keeps the planned grid-export target separate from the battery power needed by the home. Spread Export Across Window can hold the requested grid export without reducing total battery discharge to that same value, preventing a house-load increase from unnecessarily importing from the grid. PowerSync preserves the inverter's existing export limit and discharge-current settings across repeated commands, expiry, and restart recovery; systems without a safely restorable export-limit entity retain the conservative previous behavior.

**Recover FoxESS H3 battery voltage after restart ordering races**

The FoxESS entity bridge now rediscovers a supported battery-voltage entity when `foxess_modbus` publishes it after PowerSync starts, or when a previously mapped voltage becomes unavailable. Current-derived charge and discharge limits self-correct from the 500 V fallback without a manual PowerSync reload, while selected config-entry ownership and voltage-alias priority remain intact.

Update available via HACS
