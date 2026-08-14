<!-- release: v2.12.1099 -->

## What's Changed

**Show the effective self-consumption override without hiding the retained plan**

When a user or external controller selects Self Consumption during a planned charge or discharge slot, PowerSync now reports Self Consumption as the current and effective action with the measured battery power. The optimizer's retained planned action remains visible separately, so the dashboard no longer implies that blocked charging is still being commanded and the underlying economic schedule is unchanged.

**Protect and verify Tesla backup-reserve transitions**

Tesla dispatch nudges now use only a stronger temporary reserve and always drain the exact configured restore across cancellation, supersession, and orderly Home Assistant reload. At 100%, PowerSync safely reapplies and verifies 100% twice instead of lowering protection. Ordinary local and Fleet API reserve writes also require readback confirmation before they can be reported or persisted as successful, and queued reserve writers are fenced during reload.

**Reject mismatched FoxESS force-charge acknowledgements**

FoxESS remote-control power verification now handles signed 16-bit and 32-bit registers consistently. A real readback mismatch is retried and then returned as a failed hardware action so the optimizer keeps the command retryable instead of marking charge active. Firmware that exposes no usable power readback retains the existing confirmed-write compatibility path.

Update available via HACS
