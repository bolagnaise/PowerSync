## What's Changed

**Fix Sungrow SH Battery Power Sign (Always Positive)**
On some SH-series firmware, register 13022 reports battery power as always-positive, making the dashboard show discharging when the battery is actually charging (and vice versa). The fix switches to register 5214–5215 (S32 word-swapped), which is the authoritative signed battery power register used by the reference Sungrow Modbus integration — it correctly reports negative values for charging and positive for discharging. Register 13022 is kept as the initial read, with 5214 overriding it.

Update available via HACS
