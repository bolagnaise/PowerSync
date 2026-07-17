<!-- release: v2.12.883 -->
## What's Changed

**Keep Sungrow IDLE from changing the off-grid reserve**

PowerSync now holds Sungrow batteries during optimizer IDLE with only the
temporary discharge-power cap. It no longer also raises Sungrow's separate
off-grid backup SOC to the current battery level.

This prevents an elevated off-grid reserve from being stranded on Sungrow
firmware that accepts the write but does not expose a readable value for
restoration. Existing systems already showing 100% should reset that reserve
once after updating.
