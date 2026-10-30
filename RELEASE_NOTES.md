<!-- release: v2.12.1551 -->
## What's Changed

### FoxESS EV battery preservation after Force Charge

When manual Force Charge ends, PowerSync now reapplies active EV home-battery preservation on the next eligible optimiser execution. It clears the earlier `no_discharge` acknowledgement after restoring normal operation and reports the hold again only when the new command succeeds.

Update through HACS and restart Home Assistant.
