<!-- release: v2.12.1795 -->

## Sungrow solar export holds

Profit Max now reads the Sungrow inverter’s configured charge limit before pausing battery charging for solar export. When charging can resume, PowerSync restores that setting and preserves any positive limit you have since selected.

If an older hold is waiting for cleanup, PowerSync checks the inverter again after restart. A positive charge limit clears the hold. If the limit is still zero or cannot be read, cleanup stays pending so PowerSync can avoid guessing a restore value.

Update through HACS and restart Home Assistant.
