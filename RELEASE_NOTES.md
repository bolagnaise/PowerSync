<!-- release: v2.12.679 -->

## What's Changed

**Sungrow spread export now keeps inverter headroom**
PowerSync now separates the scheduled grid export target from the Sungrow inverter discharge ceiling when Spread Export Across Window is active. The optimiser still uses forced discharge for the export window, but it keeps the battery discharge limit at the normal inverter maximum and applies the spread-export target through Sungrow's feed-in/export limit register. This lets the battery continue covering home load spikes instead of importing from grid just because the spread export target is lower than the inverter capacity.

**Safer Sungrow restore handling**
The Sungrow restore path now also restores the previous export limit after a temporary spread-export command, while continuing to restore the normal charge/discharge limits. Failed spread-export writes also unwind both the export limit and discharge cap so the inverter is not left in a partial control state.

Update available via HACS
